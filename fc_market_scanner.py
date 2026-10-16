"""
EA FC market scanner: posts 6-hour price dips and spikes to a Discord webhook.

    pip install requests
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    export FC_PLATFORM=pc            # pc (default) or ps
    export FC_NOTIFY_EACH_RUN=1      # post a short "scan ran" line to Discord each run (0 = off)

    python fc_market_scanner.py                    # loop forever, scan every 15 min
    python fc_market_scanner.py --once             # one scan, then exit (GitHub Actions)
    python fc_market_scanner.py --dry-run          # print embeds instead of posting
    python fc_market_scanner.py --interval 60 --max-scans 10 --db test.db --dry-run
                                                   # fast test run with a throwaway database
    python fc_market_scanner.py --demo --dry-run   # fake data, to preview the output

How it works
  1. Each scan reads current prices from fut.gg's static price files and labels
     them using players.json (build it with build_players.py).
  2. Prices are saved to a local SQLite file (market_<platform>.db).
  3. A card is a DIP if it is >=3% below its price COMPARE_HOURS ago and below its
     usual (median over HISTORY_HOURS) price. A SPIKE is >=30% above that price.
  4. Results are grouped into price bands and posted as Discord embeds.

First alerts appear after ~COMPARE_HOURS hours (it needs a snapshot that old).
"""
import argparse
import json
import os
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass

import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
NOTIFY_EACH_RUN = os.environ.get("FC_NOTIFY_EACH_RUN", "1") == "1"   # "scan ran" line in Discord
HERE = os.path.dirname(os.path.abspath(__file__))   # data files live next to the script
PLATFORMS = {"pc": ("🖥️ PC", "pc"), "ps": ("🎮 PS", "ps5")}   # name -> (label, fut.gg file key)
PLATFORM = os.environ.get("FC_PLATFORM", "pc").lower()
if PLATFORM not in PLATFORMS:
    raise SystemExit(f"FC_PLATFORM must be one of {list(PLATFORMS)}, got {PLATFORM!r}")
PLATFORM_LABEL, PRICE_KEY = PLATFORMS[PLATFORM]
DB_PATH = os.path.join(HERE, f"market_{PLATFORM}.db")      # separate history per platform
POLL_SECONDS = 15 * 60
COMPARE_HOURS = 6                # compare today's price with this many hours ago
HISTORY_HOURS = 24               # window for "usually" / price range / 24h high
MIN_SAMPLES = 12                 # snapshots needed before a card is judged

DIP_BANDS = [(15_000, 100_000), (100_000, 500_000)]
DIP_MIN_PCT = 3.0                # drop vs COMPARE_HOURS ago
SPIKE_BAND = (20_000, 950_000)
SPIKE_MIN_PCT = 30.0             # jump vs COMPARE_HOURS ago
MAX_LINES = 8                    # per embed


@dataclass
class Card:
    card_id: str
    name: str
    rating: int
    kind: str                    # "Hero", "Icon", "TOTW", ...
    price: int
    url: str = ""
    image_url: str = ""


# ----------------------------------------------------------- fut.gg source --
FUTGG_YEAR = "27"
R2 = f"https://r2.fut.gg/{FUTGG_YEAR}/"
FUTGG_HEADERS = {"Referer": "https://www.fut.gg/",
                 "User-Agent": "Mozilla/5.0 (personal price alert bot)"}
# Player names/ratings/images. Save this by hand from your browser (Ctrl+S) at
#   https://www.fut.gg/api/fut/players/v2/27/?response_format=card-image
# That API is disallowed for bots in fut.gg's robots.txt, so the script reads
# your saved copy instead of calling it. Re-save it when new cards release.
PLAYERS_FILE = os.environ.get("FUTGG_PLAYERS_FILE", os.path.join(HERE, "players.json"))
MIN_TRACK_PRICE = 5_000          # ignore cheaper cards (keeps the DB small)
_cache: dict = {}


def _get_json(url: str):
    r = requests.get(url, headers=FUTGG_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _r2(name: str, manifest: dict):
    return _get_json(f"{R2}{name}.v1.{manifest[name]}.json")


def _load_players() -> dict[int, dict]:
    if "players" not in _cache:
        if not os.path.exists(PLAYERS_FILE):
            raise SystemExit(f"Save the fut.gg player list as {PLAYERS_FILE} "
                             "(see comments above PLAYERS_FILE).")
        with open(PLAYERS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        rows = raw["data"] if isinstance(raw, dict) else raw
        _cache["players"] = {row["eaId"]: row for row in rows}
        print(f"[info] loaded {len(_cache['players'])} players from {PLAYERS_FILE}")
    return _cache["players"]


def _load_ids(manifest: dict, players: dict) -> list[int]:
    """Price index = first id + gaps between sorted ids (delta encoding)."""
    h = manifest["player-prices-index"]
    if _cache.get("ids_hash") != h:
        idx = _r2("player-prices-index", manifest)
        ids = [idx["id0"]]
        for gap in idx["d"]:
            ids.append(ids[-1] + gap)
        found = sum(i in players for i in ids)
        if players and found < 0.8 * len(players):     # decoding sanity check
            raise RuntimeError(
                f"Only {found}/{len(players)} players matched the price index "
                "- the file format may have changed.")
        _cache["ids_hash"], _cache["ids"] = h, ids
    return _cache["ids"]


def fetch_market() -> list[Card]:
    """Current prices for tracked cards, from fut.gg's static price files."""
    manifest = _get_json(R2 + "manifest.json")      # maps names -> current hashes
    players = _load_players()
    ids = _load_ids(manifest, players)
    prices = _r2(f"player-prices-{PRICE_KEY}-dyn", manifest)["p"]
    if len(prices) != len(ids):
        raise RuntimeError(f"{len(prices)} prices vs {len(ids)} ids - files out of sync")
    cards = []
    for ea_id, price in zip(ids, prices):
        meta = players.get(ea_id)
        if meta is None or price < MIN_TRACK_PRICE:
            continue                                 # unknown card, no price or cheap
        cards.append(Card(str(ea_id), meta["commonName"], meta["overall"],
                          meta["rarityName"], price,
                          "https://www.fut.gg" + meta["url"], meta["cardImageUrl"]))
    return cards


# ---------------------------------------------------------------- storage --
def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("create table if not exists prices(card_id text, ts real, price int)")
    db.execute("create index if not exists ix on prices(card_id, ts)")
    return db


def save_snapshot(db, cards: list[Card], now: float) -> None:
    db.executemany("insert into prices values (?,?,?)",
                   [(c.card_id, now, c.price) for c in cards])
    db.execute("delete from prices where ts < ?", (now - (HISTORY_HOURS + 12) * 3600,))
    db.commit()


def card_stats(db, card: Card, now: float):
    rows = db.execute(
        "select ts, price from prices where card_id=? and ts>=? order by ts",
        (card.card_id, now - HISTORY_HOURS * 3600)).fetchall()
    if len(rows) < MIN_SAMPLES:
        return None
    older = [p for t, p in rows if t <= now - COMPARE_HOURS * 3600]
    if not older:
        return None
    prices = [p for _, p in rows]
    q = statistics.quantiles(prices, n=4)
    return {
        "ref": older[-1],                       # newest price >= COMPARE_HOURS old
        "usual": statistics.median(prices),
        "low": q[0],
        "high": q[2],
        "high24": max(p for t, p in rows if t >= now - 24 * 3600),
    }


# ---------------------------------------------------------------- formats --
def k(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(int(n))


def tier_emoji(pct: float) -> str:
    a = abs(pct)
    return "🟠" if a >= 10 else "🟢" if a >= 5 else "🟡"


def link(c: Card) -> str:
    return f"[{c.name}]({c.url})" if c.url else f"**{c.name}**"


def build_dips(items) -> list[dict]:
    embeds = []
    for lo, hi in DIP_BANDS:
        band = sorted((i for i in items if lo <= i[0].price < hi),
                      key=lambda i: i[2])[:MAX_LINES]
        if not band:
            continue
        lines = [f"**{k(lo)} – {k(hi)}**"]
        for c, s, pct in band:
            lines.append(f"{tier_emoji(pct)} `{pct:+.1f}%` **{k(c.price)}** "
                         f"{link(c)} {c.rating} {c.kind}")
            lines.append(f"buy under **{k(c.price)}** · usually {k(s['usual'])} "
                         f"| {k(s['low'])} – {k(s['high'])} | {HISTORY_HOURS}h")
        lines += ["", "🟠 10%+ · 🟢 5–10% · 🟡 3–5%"]
        e = {"title": f"⚡ {COMPARE_HOURS} Hour dips {PLATFORM_LABEL}",
             "description": "\n".join(lines),
             "color": 0x2ECC71,
             "footer": {"text": f"{COMPARE_HOURS} Hour dips"}}
        if band[0][0].image_url:
            e["thumbnail"] = {"url": band[0][0].image_url}
        embeds.append(e)
    return embeds


def build_spikes(items) -> list[dict]:
    items = sorted(items, key=lambda i: -i[2])[:MAX_LINES]
    if not items:
        return []
    lines = [f"{len(items)} cards jumped {SPIKE_MIN_PCT:.0f}%+ in the last {COMPARE_HOURS}h", ""]
    for c, s, pct in items:
        verdict = ("well above usual – sell" if c.price > s["usual"] * 1.3
                   else "above usual – sell")
        lines.append(f"🔵 `▲{pct:.0f}%` {link(c)} {c.rating} {c.kind} · "
                     f"~~{k(s['ref'])}~~ → **{k(c.price)}**")
        lines.append(f"24h high {k(s['high24'])} · **{verdict}**")
    e = {"title": f"🚀 Spikes · last {COMPARE_HOURS}h · {k(SPIKE_BAND[0])}–{k(SPIKE_BAND[1])} {PLATFORM_LABEL}",
         "description": "\n".join(lines),
         "color": 0xE67E22,
         "footer": {"text": "Spikes"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


# ------------------------------------------------------------------- scan --
def scan(db, cards: list[Card], now: float) -> list[dict]:
    dips, spikes = [], []
    for c in cards:
        s = card_stats(db, c, now)
        if not s or s["ref"] <= 0:
            continue
        pct = (c.price - s["ref"]) / s["ref"] * 100
        if pct <= -DIP_MIN_PCT and c.price < s["usual"]:
            dips.append((c, s, pct))
        if pct >= SPIKE_MIN_PCT and SPIKE_BAND[0] <= c.price <= SPIKE_BAND[1]:
            spikes.append((c, s, pct))
    return build_dips(dips) + build_spikes(spikes)


def post(embeds: list[dict], dry_run: bool) -> None:
    for e in embeds:
        if dry_run:
            print(json.dumps(e, ensure_ascii=False, indent=2))
            continue
        r = requests.post(WEBHOOK_URL, json={"embeds": [e]}, timeout=10)
        r.raise_for_status()
        time.sleep(1)


# ------------------------------------------------------------------- demo --
def demo_seed(db, now: float):
    """Fake history for 40 cards; a few dip, a few spike at the end."""
    rnd = random.Random(7)
    kinds = ["Hero", "Icon", "TOTW", "Rare Gold"]
    cards = []
    for i in range(40):
        base = rnd.choice([30_000, 55_000, 70_000, 250_000, 450_000])
        cards.append(Card(str(i), f"Demo Player {i}", rnd.randint(84, 91),
                          rnd.choice(kinds), base, url="https://example.com"))
    steps = HISTORY_HOURS * 2
    for step in range(steps, -1, -1):
        ts = now - step * 1800
        for c in cards:
            p = c.price * (1 + rnd.uniform(-0.015, 0.015))
            if step <= 4:                              # last ~2h: moves
                if int(c.card_id) % 8 == 0:
                    p = c.price * 0.9                  # dip
                if int(c.card_id) % 13 == 0:
                    p = c.price * 1.45                 # spike
            db.execute("insert into prices values (?,?,?)",
                       (c.card_id, ts, int(p)))
    db.commit()
    now_cards = []
    for c in cards:
        p = c.price
        if int(c.card_id) % 8 == 0:
            p = int(c.price * 0.9)
        if int(c.card_id) % 13 == 0:
            p = int(c.price * 1.45)
        now_cards.append(Card(c.card_id, c.name, c.rating, c.kind, p, c.url))
    return now_cards


def notice(text: str, dry_run: bool) -> None:
    """Short status line in Discord so you can see each run happen."""
    if not NOTIFY_EACH_RUN:
        return
    try:
        if dry_run:
            print(f"[notice] {text}")
        else:
            requests.post(WEBHOOK_URL, json={"content": text}, timeout=10).raise_for_status()
    except Exception as e:                # a failed notice must never fail the scan
        print(f"[warn] could not post notice: {e}")


def fmt_gap(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h{m % 60:02d}m"


def run_cycle(db, dry_run: bool) -> None:
    now = time.time()
    prev = db.execute("select max(ts) from prices").fetchone()[0]   # last saved scan
    try:
        cards = fetch_market()
        embeds = scan(db, cards, now)     # judge against history first
        post(embeds, dry_run)
        save_snapshot(db, cards, now)
    except Exception as e:
        notice(f"⚠️ {PLATFORM_LABEL} scan failed: {str(e)[:300]}", dry_run)
        raise
    stamp = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[info] {stamp} UTC scanned {len(cards)} cards, posted {len(embeds)} embed(s)")
    gap = f" · {fmt_gap(now - prev)} since last scan" if prev else " · first scan"
    via = os.environ.get("GITHUB_EVENT_NAME", "")      # set by GitHub Actions
    via = f" · via {via}" if via else ""
    notice(f"🔄 {PLATFORM_LABEL} scan ran at {stamp} UTC · {len(cards)} cards · "
           f"{len(embeds)} alert embed(s){gap}{via}", dry_run)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print, don't send")
    ap.add_argument("--once", action="store_true",
                    help="one scan then exit (for GitHub Actions / cron)")
    ap.add_argument("--interval", type=float, default=POLL_SECONDS,
                    help="seconds between scans in loop mode (minimum 30)")
    ap.add_argument("--max-scans", type=int, default=0,
                    help="stop after this many scans (0 = run forever)")
    ap.add_argument("--db", default=DB_PATH, help="price history database file")
    args = ap.parse_args()
    if args.interval < 30:
        raise SystemExit("--interval must be at least 30 seconds (be kind to fut.gg)")

    if not args.dry_run and not WEBHOOK_URL:
        raise SystemExit("Set DISCORD_WEBHOOK_URL (or use --dry-run)")

    if args.demo:
        db = open_db(":memory:")
        now = time.time()
        cards = demo_seed(db, now)
        embeds = scan(db, cards, now)
        print(f"[demo] {len(embeds)} embed(s)")
        post(embeds, args.dry_run)
        return

    db = open_db(args.db)
    if args.once:
        run_cycle(db, args.dry_run)       # errors fail the run, visible in Actions
        return
    done = failed = 0
    while True:
        try:
            run_cycle(db, args.dry_run)
        except Exception as e:
            failed += 1
            print(f"[warn] {e}")
        done += 1
        if args.max_scans and done >= args.max_scans:
            break
        time.sleep(args.interval)
    if failed == done:
        raise SystemExit("every scan failed")


if __name__ == "__main__":
    main()
