"""
EA FC market scanner: posts 6-hour price dips and spikes to a Discord webhook.

    Premium cards (Icons and Heroes) get their own dip detector (24h sales
    average + liquidity gate). fut.gg's data has no holo flag, so holo cards are
    labelled from your own list in holo_ids.json (see HOLO_FILE below).
    TOTW cards below TOTW_MIN_PRICE (15k) are skipped from dip/spike alerts
    to eliminate min-price fluctuation noise.

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
    python fc_market_scanner.py --watchlist watchlist.json   # enable watchlist alerts

How it works
  1. Each scan reads current prices from fut.gg's static price files and labels
     them using players.json (build it with build_players.py).
  2. Prices are saved to a local SQLite file (market_<platform>.db).
  3. A card is a DIP if it is >=8% below its price COMPARE_HOURS ago and below its
     usual (median over HISTORY_HOURS) price. A SPIKE is >=8% above that price.
  4. MICRO-SPIKES / CRASHES compare the last two consecutive snapshots (~15 min
     apart) and fire immediately without waiting for COMPARE_HOURS of history.
  5. WATCHLIST cards are always posted with their current price each scan,
     regardless of dip/spike thresholds.
  6. Results are grouped into price bands and posted as Discord embeds.

First 6h alerts appear after ~COMPARE_HOURS hours (it needs a snapshot that old).
Micro-spike / crash alerts fire as soon as 2 snapshots exist for a card (~15 min).
"""
import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass

import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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

# ---- premium card settings (Icons, Heroes) ---------------------------------
# A card is premium if its fut.gg rarity name contains one of these words
# (case-insensitive). The real names are e.g. "Base Icon", "Debut International
# Icon" and "Base Hero", so exact-name matching would never match. "Base Hall of
# FUT" is not included; add "hall of fut" here if you want it.
PREMIUM_WORDS = ("icon", "hero")
# 24 h window: enough history without diluting recent price moves
PREMIUM_HISTORY_HOURS = 24
# A premium dip must be this deep vs the 24 h median to be worth alerting
PREMIUM_DIP_MIN_PCT = 8.0
# Liquidity gate: at least this many consecutive-snapshot price changes in 24 h
# (proxy for active trading – icons that don't move are untradeable at the dip)
PREMIUM_MIN_PRICE_CHANGES = 4
# Minimum snapshots required in the 24 h window for the liquidity check
PREMIUM_MIN_SNAPSHOTS_24H = 8

def parse_price(val: str | int | float | None) -> int | None:
    """Parse an integer or shorthand price string ('15k', '1.5M') into an int."""
    if val is None or val == "" or str(val).strip().lower() in ("none", "0", "unlimited"):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().lower().replace(",", "")
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("k"):
        return int(float(s[:-1]) * 1_000)
    return int(float(s))


# Price range for cards of interest (set via FC_MIN_PRICE / FC_MAX_PRICE or --min-price / --max-price)
# Cards outside this range are ignored for alerts. Supports e.g. "15k", "1.5M", 50000.
MIN_PRICE = parse_price(os.environ.get("FC_MIN_PRICE", 15_000))
MAX_PRICE = parse_price(os.environ.get("FC_MAX_PRICE", None))  # None = no upper bound

# ---- TOTW fodder filter ----------------------------------------------------
# TOTW cards below this price produce noisy min-price fluctuations → skip them
TOTW_MIN_PRICE = 15_000
# fut.gg's rarity name is "Team of the week" (lower-case w), so match loosely.

DIP_BANDS = [
    (15_000, 100_000),
    (100_000, 500_000),
    (500_000, 1_500_000),
    (1_500_000, 15_000_000),
]
DIP_MIN_PCT = 8.0                # drop vs COMPARE_HOURS ago
SPIKE_BAND = (20_000, 950_000)
SPIKE_MIN_PCT = 8.0              # jump vs COMPARE_HOURS ago
MAX_LINES = int(os.environ.get("FC_MAX_LINES", 16))   # per embed (0 = unlimited)

def _limit(items: list) -> list:
    """Limit the number of cards shown per embed/section, or return all if MAX_LINES <= 0."""
    return items[:MAX_LINES] if MAX_LINES > 0 else items

# ---- micro-spike / dip / crash (single scan-to-scan delta, ~15 min window) -
FLASH_SPIKE_PCT = 8.0            # jump between last two snapshots to count as micro-spike
FLASH_DIP_PCT = 8.0              # drop between last two snapshots to count as micro-dip
FLASH_CRASH_PCT = 12.0           # drop threshold that escalates a micro-dip to a crash
FLASH_MIN_PRICE = 10_000         # ignore cheap cards for flash alerts

# ---- watchlist -------------------------------------------------------------
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json") if False else None  # set via --watchlist


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
# Optional: holo_ids.json = a JSON list of card (EA) ids to label as holographic,
# e.g. [162895, 50578336]. The id is the last number in the card's fut.gg URL
# (.../players/226362-louisa-necib/27-84112442/ -> 84112442). A holo card is
# tracked as its own card only if it has its own id; if it shares the id of the
# normal card there is just one price and it can only be labelled.
HOLO_FILE = os.environ.get("FC_HOLO_FILE", os.path.join(HERE, "holo_ids.json"))
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


def _load_holo(players: dict) -> set[int]:
    if "holo" not in _cache:
        ids: set[int] = set()
        if os.path.exists(HOLO_FILE):
            with open(HOLO_FILE, encoding="utf-8") as f:
                ids = {int(i) for i in json.load(f)}
            missing = sorted(i for i in ids if i not in players)
            print(f"[info] {len(ids)} holo id(s) loaded from {HOLO_FILE}")
            if missing:
                print(f"[warn] holo ids not in players.json (add their pages): {missing}")
        _cache["holo"] = ids
    return _cache["holo"]


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
    holo = _load_holo(players)
    prices = _r2(f"player-prices-{PRICE_KEY}-dyn", manifest)["p"]
    if len(prices) != len(ids):
        raise RuntimeError(f"{len(prices)} prices vs {len(ids)} ids - files out of sync")
    cards = []
    for ea_id, price in zip(ids, prices):
        meta = players.get(ea_id)
        if meta is None or price < MIN_TRACK_PRICE:
            continue                                 # unknown card, no price or cheap
        kind = meta["rarityName"] + (" 🃏 Holo" if ea_id in holo else "")
        cards.append(Card(str(ea_id), meta["commonName"], meta["overall"],
                          kind, price,
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


def card_flash_stats(db, card: Card):
    """Return the previous scan's price and timestamp, or None.

    Used for micro-spike / crash detection: compares only the last two
    consecutive snapshots, so alerts fire within one scan interval (~15 min)
    without needing COMPARE_HOURS of history.
    """
    # scan() runs BEFORE the current prices are saved, so the newest saved row
    # is the previous scan, which is the baseline to compare against.
    rows = db.execute(
        "select ts, price from prices where card_id=? order by ts desc limit 1",
        (card.card_id,)).fetchall()
    if not rows:
        return None
    prev_ts, prev_price = rows[0]
    return {"prev_price": prev_price, "prev_ts": prev_ts}


def load_watchlist(path: str | None) -> set[str]:
    """Load a JSON file containing a list of EA card IDs to always track.

    The file should be a JSON array of strings or ints, e.g.:
        ["158023", "20801", 231747]

    Returns an empty set if path is None or the file does not exist.
    """
    if not path:
        return set()
    if not os.path.exists(path):
        print(f"[warn] watchlist file not found: {path}")
        return set()
    with open(path, encoding="utf-8") as f:
        ids = json.load(f)
    result = {str(i) for i in ids}
    print(f"[info] watchlist loaded: {len(result)} card(s) from {path}")
    return result


def extract_recent_sales(rows: list[tuple[float, int]]) -> list[int]:
    """Extract distinct sale/trade price points by filtering consecutive identical prices.

    In the FC transfer market, when a card sells and clears the market,
    the live lowest BIN shifts to the next listed card. Deduplicating
    consecutive identical snapshot prices isolates the sequence of actual
    sales and market price changes.
    """
    if not rows:
        return []
    sales = [rows[0][1]]
    for _, price in rows[1:]:
        if price != sales[-1]:
            sales.append(price)
    return sales


def card_stats(db, card: Card, now: float, history_hours: int = HISTORY_HOURS):
    """Return price stats for a card over *history_hours*.

    Non-premium cards use the default HISTORY_HOURS window.
    Premium cards are handled by card_premium_dip_stats() instead.
    """
    rows = db.execute(
        "select ts, price from prices where card_id=? and ts>=? order by ts",
        (card.card_id, now - history_hours * 3600)).fetchall()
    if len(rows) < MIN_SAMPLES:
        return None
    older = [p for t, p in rows if t <= now - COMPARE_HOURS * 3600]
    if not older:
        return None
    prices = [p for _, p in rows]
    sales = extract_recent_sales(rows)
    sales_avg = int(statistics.mean(sales)) if sales else int(statistics.median(prices))
    sales_count = max(0, len(sales) - 1)
    q = statistics.quantiles(prices, n=4)
    return {
        "ref": older[-1],                       # newest price >= COMPARE_HOURS old
        "usual": statistics.median(prices),
        "sales_avg": sales_avg,
        "sales_count": sales_count,
        "low": q[0],
        "high": q[2],
        "high24": max(p for t, p in rows if t >= now - 24 * 3600),
    }


def card_premium_dip_stats(db, card: Card, now: float) -> dict | None:
    """Specialised stats for premium-card dip detection using recent sales history.

    Criteria applied here (all must pass):
    1. Enough snapshots in the 24 h window (PREMIUM_MIN_SNAPSHOTS_24H).
    2. Liquidity / Sales demand: at least PREMIUM_MIN_PRICE_CHANGES actual sales
       (price changes) in the 24h window. A card whose price never moved is
       illiquid and lacks buyer demand.
    3. Price dip: current price is at least PREMIUM_DIP_MIN_PCT below the
       24h Recent Sales Average (mean of actual traded price points).

    Returns a dict with stats, or None if any gate fails.
    """
    rows = db.execute(
        "select ts, price from prices where card_id=? and ts>=? order by ts",
        (card.card_id, now - PREMIUM_HISTORY_HOURS * 3600)).fetchall()
    if len(rows) < PREMIUM_MIN_SNAPSHOTS_24H:
        return None                              # not enough data yet

    prices = [p for _, p in rows]
    sales = extract_recent_sales(rows)
    sales_count = max(0, len(sales) - 1)

    # ── liquidity / trading activity gate ───────────────────────────────────
    if sales_count < PREMIUM_MIN_PRICE_CHANGES:
        return None                              # price barely moved – illiquid

    # ── recent sales average ────────────────────────────────────────────────
    # Mean of the actual traded prices (filtering out stale idle periods)
    sales_avg = int(statistics.mean(sales))
    if sales_avg <= 0:
        return None

    dip_pct = (card.price - sales_avg) / sales_avg * 100
    if dip_pct > -PREMIUM_DIP_MIN_PCT:
        return None                              # not enough of a dip vs sales avg

    q = statistics.quantiles(prices, n=4)
    return {
        "sales_avg": sales_avg,
        "sales_median": int(statistics.median(sales)),
        "usual": statistics.median(prices),
        "low": q[0],
        "high": q[2],
        "dip_pct": dip_pct,
        "sales_count": sales_count,
        "snapshots": len(rows),
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
    return "🔴" if a >= 15 else "🟠" if a >= 12 else "🟢"


def link(c: Card) -> str:
    return f"[{c.name}]({c.url})" if c.url else f"**{c.name}**"


def build_dips(items) -> list[dict]:
    embeds = []
    for lo, hi in DIP_BANDS:
        band = _limit(sorted((i for i in items if lo <= i[0].price < hi),
                             key=lambda i: i[2]))
        if not band:
            continue
        lines = [f"**{k(lo)} – {k(hi)}**"]
        for c, s, pct in band:
            lines.append(f"{tier_emoji(pct)} `{pct:+.1f}%` **{k(c.price)}** "
                         f"{link(c)} {c.rating} {c.kind}")
            lines.append(f"buy under **{k(c.price)}** · sales avg {k(s['sales_avg'])} (usual {k(s['usual'])}) "
                         f"| {k(s['low'])} – {k(s['high'])} | {s['sales_count']} sales")
        lines += ["", "🔴 15%+ · 🟠 12–15% · 🟢 8–12%"]
        e = {"title": f"⚡ {COMPARE_HOURS} Hour dips {PLATFORM_LABEL}",
             "description": "\n".join(lines),
             "color": 0x2ECC71,
             "footer": {"text": f"{COMPARE_HOURS} Hour dips · recent sales avg"}}
        if band[0][0].image_url:
            e["thumbnail"] = {"url": band[0][0].image_url}
        embeds.append(e)
    return embeds


def build_spikes(items) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: -i[2]))
    if not items:
        return []
    lines = [f"{len(items)} cards jumped {SPIKE_MIN_PCT:.0f}%+ in the last {COMPARE_HOURS}h", ""]
    for c, s, pct in items:
        verdict = ("well above usual – sell" if c.price > s["usual"] * 1.3
                   else "above usual – sell")
        lines.append(f"🔵 `▲{pct:.0f}%` {link(c)} {c.rating} {c.kind} · "
                     f"~~{k(s['ref'])}~~ → **{k(c.price)}**")
        lines.append(f"24h high {k(s['high24'])} · **{verdict}**")
    range_label = f" · {k(MIN_PRICE or 15_000)}–{k(MAX_PRICE) if MAX_PRICE else '∞'}"
    e = {"title": f"🚀 Spikes · last {COMPARE_HOURS}h{range_label} {PLATFORM_LABEL}",
         "description": "\n".join(lines),
         "color": 0xE67E22,
         "footer": {"text": "Spikes"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


def build_flashes(spike_items, dip_items, crash_items) -> list[dict]:
    """Embeds for micro-spikes, micro-dips, and crashes detected between the last two scans."""
    embeds = []

    if spike_items:
        spike_items = _limit(sorted(spike_items, key=lambda i: -i[2]))
        lines = [f"{len(spike_items)} card(s) jumped ≥{FLASH_SPIKE_PCT:.0f}% in the last scan (~15 min)", ""]
        for c, f, pct in spike_items:
            lines.append(f"⚡ `▲{pct:.1f}%` **{k(c.price)}** {link(c)} {c.rating} {c.kind}")
            lines.append(f"~~{k(f['prev_price'])}~~ → **{k(c.price)}**")
        e = {"title": f"⚡ Micro-Spikes · {PLATFORM_LABEL}",
             "description": "\n".join(lines),
             "color": 0xF1C40F,
             "footer": {"text": "Micro-spike (last scan delta)"}}
        if spike_items[0][0].image_url:
            e["thumbnail"] = {"url": spike_items[0][0].image_url}
        embeds.append(e)

    if dip_items:
        dip_items = _limit(sorted(dip_items, key=lambda i: i[2]))   # most negative first
        lines = [f"{len(dip_items)} card(s) dipped ≥{FLASH_DIP_PCT:.0f}% in the last scan (~15 min)", ""]
        for c, f, pct in dip_items:
            lines.append(f"📉 `{pct:.1f}%` **{k(c.price)}** {link(c)} {c.rating} {c.kind}")
            lines.append(f"~~{k(f['prev_price'])}~~ → **{k(c.price)}** · possible buy window")
        e = {"title": f"📉 Micro-Dips · {PLATFORM_LABEL}",
             "description": "\n".join(lines),
             "color": 0x1ABC9C,
             "footer": {"text": f"Micro-dip ({FLASH_DIP_PCT:.0f}–{FLASH_CRASH_PCT:.0f}% drop, last scan delta)"}}
        if dip_items[0][0].image_url:
            e["thumbnail"] = {"url": dip_items[0][0].image_url}
        embeds.append(e)

    if crash_items:
        crash_items = _limit(sorted(crash_items, key=lambda i: i[2]))  # most negative first
        lines = [f"{len(crash_items)} card(s) crashed ≥{FLASH_CRASH_PCT:.0f}% in the last scan (~15 min)", ""]
        for c, f, pct in crash_items:
            lines.append(f"💥 `{pct:.1f}%` **{k(c.price)}** {link(c)} {c.rating} {c.kind}")
            lines.append(f"~~{k(f['prev_price'])}~~ → **{k(c.price)}** · possible dump")
        e = {"title": f"💥 Price Crashes · {PLATFORM_LABEL}",
             "description": "\n".join(lines),
             "color": 0xE74C3C,
             "footer": {"text": "Crash (last scan delta)"}}
        if crash_items[0][0].image_url:
            e["thumbnail"] = {"url": crash_items[0][0].image_url}
        embeds.append(e)

    return embeds


def build_premium_dips(items) -> list[dict]:
    """Dedicated embed for Icon / Hero dip opportunities.

    Each entry already passed the 24 h sales avg + liquidity gate in
    card_premium_dip_stats(), so everything shown here is a real signal.
    """
    items = _limit(sorted(items, key=lambda i: i[1]["dip_pct"]))   # deepest first
    if not items:
        return []
    lines = [
        f"{len(items)} premium card(s) dipped ≥{PREMIUM_DIP_MIN_PCT:.0f}% vs 24 h sales avg",
        f"Liquidity gate: ≥{PREMIUM_MIN_PRICE_CHANGES} sales in last 24 h · {PREMIUM_HISTORY_HOURS}h window",
        "",
    ]
    for c, s in items:
        liq_bar = "🔥" * min(max(s["sales_count"] // 3, 1), 5)   # visual liquidity meter
        lines.append(
            f"{tier_emoji(s['dip_pct'])} `{s['dip_pct']:+.1f}%` **{k(c.price)}** "
            f"{link(c)} {c.rating} · *{c.kind}*"
        )
        lines.append(
            f"sales avg **{k(s['sales_avg'])}** (median {k(s['sales_median'])}) "
            f"· range {k(s['low'])}–{k(s['high'])} "
            f"· {s['sales_count']} sales {liq_bar}"
        )
    lines += ["", "🔴 15%+ · 🟠 12–15% · 🟢 8–12%"]
    e = {
        "title": f"💎 Premium Dips (Icon / Hero) · {PLATFORM_LABEL}",
        "description": "\n".join(lines),
        "color": 0x8E44AD,
        "footer": {"text": (
            f"24 h sales avg · ≥{PREMIUM_DIP_MIN_PCT:.0f}% dip · "
            f"≥{PREMIUM_MIN_PRICE_CHANGES} sales"
        )},
    }
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


def build_watchlist(cards_map: dict[str, Card], watchlist: set[str]) -> list[dict]:
    """Embed showing current price for every card on the watchlist."""
    hits = [cards_map[cid] for cid in watchlist if cid in cards_map]
    if not hits:
        return []
    hits = sorted(hits, key=lambda c: -c.price)
    lines = []
    for c in hits:
        lines.append(f"👁 **{k(c.price)}** {link(c)} {c.rating} {c.kind}")
    e = {"title": f"👁 Watchlist · {PLATFORM_LABEL}",
         "description": "\n".join(lines),
         "color": 0x9B59B6,
         "footer": {"text": "Watchlist – current prices"}}
    return [e]


# ------------------------------------------------------------------- scan --
def _is_premium(card: Card) -> bool:
    """True for Icons and Heroes (matched on words in the rarity name)."""
    kind = card.kind.lower()
    return any(word in kind for word in PREMIUM_WORDS)


def _is_totw(card: Card) -> bool:
    kind = card.kind.lower()
    return "team of the week" in kind or kind.startswith("totw")


def _is_totw_fodder(card: Card) -> bool:
    """True for TOTW cards below the minimum price threshold.

    These cards tend to sit at EA's minimum price and generate spurious
    min-price fluctuation alerts that add noise without actionable signal.
    """
    return _is_totw(card) and card.price < TOTW_MIN_PRICE


def scan(db, cards: list[Card], now: float, watchlist: set[str] | None = None) -> list[dict]:
    dips, spikes, flash_spikes, flash_dips, flash_crashes = [], [], [], [], []
    premium_dips: list[tuple[Card, dict]] = []   # (card, stats) – own embed
    cards_map = {c.card_id: c for c in cards}

    for c in cards:
        # ── filter cards outside price range of interest ────────────────────
        if MIN_PRICE is not None and c.price < MIN_PRICE:
            continue
        if MAX_PRICE is not None and c.price > MAX_PRICE:
            continue

        # ── skip TOTW fodder at min price (noisy, no signal) ────────────────
        if _is_totw_fodder(c):
            continue

        if _is_premium(c):
            # ── premium path: dedicated dip detector (24 h, liquidity-gated) ─
            ps = card_premium_dip_stats(db, c, now)
            if ps is not None:
                premium_dips.append((c, ps))

            # Premium cards still participate in spike and flash alerts.
            # They are intentionally excluded from the general dip list to
            # avoid duplicate / lower-quality alerts for the same card.
            s = card_stats(db, c, now)
            if s and s["ref"] > 0:
                pct = (c.price - s["ref"]) / s["ref"] * 100
                if pct >= SPIKE_MIN_PCT:
                    spikes.append((c, s, pct))
        else:
            # ── standard path: 6h dip / spike ───────────────────────────────
            s = card_stats(db, c, now)
            if s and s["ref"] > 0:
                pct = (c.price - s["ref"]) / s["ref"] * 100
                if pct <= -DIP_MIN_PCT and c.price < s["usual"] and c.price <= s["sales_avg"]:
                    dips.append((c, s, pct))
                if pct >= SPIKE_MIN_PCT:
                    spikes.append((c, s, pct))

        # ── micro-spike / dip / crash (last two snapshots) ───────────────────
        if c.price >= FLASH_MIN_PRICE:
            f = card_flash_stats(db, c)
            if f and f["prev_price"] > 0:
                pct = (c.price - f["prev_price"]) / f["prev_price"] * 100
                if pct >= FLASH_SPIKE_PCT:
                    flash_spikes.append((c, f, pct))
                elif pct <= -FLASH_CRASH_PCT:
                    flash_crashes.append((c, f, pct))
                elif pct <= -FLASH_DIP_PCT:
                    flash_dips.append((c, f, pct))

    embeds = (
        build_dips(dips)
        + build_spikes(spikes)
        + build_flashes(flash_spikes, flash_dips, flash_crashes)
        + build_premium_dips(premium_dips)    # 💎 Icon / Hero dips
    )

    # ── watchlist ────────────────────────────────────────────────────────────
    if watchlist:
        embeds += build_watchlist(cards_map, watchlist)

    return embeds


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
                    p = c.price * 0.88                 # dip (>=10%)
                if int(c.card_id) % 13 == 0:
                    p = c.price * 1.45                 # spike
            db.execute("insert into prices values (?,?,?)",
                       (c.card_id, ts, int(p)))
    db.commit()
    now_cards = []
    for c in cards:
        p = c.price
        if int(c.card_id) % 8 == 0:
            p = int(c.price * 0.88)
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


def run_cycle(db, dry_run: bool, watchlist: set[str] | None = None) -> None:
    now = time.time()
    prev = db.execute("select max(ts) from prices").fetchone()[0]   # last saved scan
    try:
        cards = fetch_market()
        embeds = scan(db, cards, now, watchlist)   # judge against history first
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
    global MAX_LINES, MIN_PRICE, MAX_PRICE
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
    ap.add_argument("--watchlist", default=None, metavar="FILE",
                    help="JSON file with a list of card IDs to always post prices for")
    ap.add_argument("--max-lines", type=int, default=MAX_LINES,
                    help="maximum number of cards to display per section/embed (default: 16, 0 = unlimited)")
    ap.add_argument("--min-price", default=None,
                    help="minimum card price of interest (e.g. 20000 or '20k')")
    ap.add_argument("--max-price", default=None,
                    help="maximum card price of interest (e.g. 1000000 or '1M')")
    args = ap.parse_args()
    if args.max_lines is not None:
        MAX_LINES = args.max_lines
    if args.min_price is not None:
        MIN_PRICE = parse_price(args.min_price)
    if args.max_price is not None:
        MAX_PRICE = parse_price(args.max_price)
    watchlist = load_watchlist(args.watchlist)
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
        run_cycle(db, args.dry_run, watchlist)       # errors fail the run, visible in Actions
        return
    done = failed = 0
    while True:
        try:
            run_cycle(db, args.dry_run, watchlist)
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
