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
    python fc_market_scanner.py --once --runs 3 --run-gap 300   # 3 scans per launch
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
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json")   # auto-loaded if it exists
UNLISTED: list = []   # watchlist cards with no current listing (filled by fetch_market)


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


def fetch_market(watchlist: set[str] | None = None) -> list[Card]:
    """Current prices for tracked cards, from fut.gg's static price files."""
    manifest = _get_json(R2 + "manifest.json")      # maps names -> current hashes
    players = _load_players()
    ids = _load_ids(manifest, players)
    holo = _load_holo(players)
    prices = _r2(f"player-prices-{PRICE_KEY}-dyn", manifest)["p"]
    if len(prices) != len(ids):
        raise RuntimeError(f"{len(prices)} prices vs {len(ids)} ids - files out of sync")
    cards = []
    UNLISTED.clear()
    watch = watchlist or set()
    for ea_id, price in zip(ids, prices):
        meta = players.get(ea_id)
        if meta is None:
            continue                                 # unknown card
        watched = str(ea_id) in watch
        if price < MIN_TRACK_PRICE and not watched:
            continue                                 # no price or cheap
        kind = meta["rarityName"] + (" 🃏 Holo" if ea_id in holo else "")
        card = Card(str(ea_id), meta["commonName"], meta["overall"], kind, price,
                    "https://www.fut.gg" + meta["url"], meta["cardImageUrl"])
        if price <= 0:
            UNLISTED.append(card)                    # watched but nothing on the market
        else:
            cards.append(card)
    return cards


# ---------------------------------------------------------------- storage --
def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("create table if not exists prices(card_id text, ts real, price int)")
    db.execute("create index if not exists ix on prices(card_id, ts)")
    db.execute("create table if not exists prices_hourly(card_id text, hour_ts real, price int, "
               "primary key(card_id, hour_ts))")
    db.execute("create table if not exists outcomes(id integer primary key autoincrement, "
               "card_id text, kind text, ts real, price int, depth real, mkt real, "
               "g text, band text, dbin text, dtype text, "
               "r1 real, r3 real, r6 real, done int default 0)")
    if "slot" not in {r[1] for r in db.execute("pragma table_info(outcomes)")}:
        db.execute("alter table outcomes add column slot text")
    db.execute("create table if not exists market_ret(g text, ts real, ret real)")
    db.execute("create index if not exists mx on market_ret(g, ts)")
    db.execute("create index if not exists ox on outcomes(card_id, ts)")
    db.execute("create table if not exists seen(card_id text primary key, first_ts real)")
    db.execute("create table if not exists watch_auto(card_id text primary key, added_ts real)")
    db.execute("create table if not exists alerts(card_id text, kind text, ts real, price int)")
    return db


# ---- new-card filter ---------------------------------------------------------
# Freshly released cards crash/swing wildly in their first days, which floods the
# alerts. A card counts as "new" for NEW_CARD_DAYS after it first shows up in
# players.json. The first run on an empty DB treats every existing card as old.
NEW_CARD_DAYS = float(os.environ.get("FC_NEW_CARD_DAYS", "4"))
# Safety valve: if more than this many ids appear at once it is a bulk update of
# players.json (re-running setup, adding holos), not a promo drop - treat them as old.
NEW_CARD_MAX_BATCH = int(os.environ.get("FC_NEW_CARD_MAX_BATCH", "120"))


def register_cards(db, ids, now: float) -> None:
    """Record when each card id was first seen in the player list."""
    empty = db.execute("select 1 from seen limit 1").fetchone() is None
    known = {r[0] for r in db.execute("select card_id from seen")}
    fresh = [str(i) for i in ids if str(i) not in known]
    bulk = len(fresh) > NEW_CARD_MAX_BATCH
    ts = 0.0 if (empty or bulk) else now
    if fresh:
        db.executemany("insert into seen values (?,?)", [(i, ts) for i in fresh])
        db.commit()
        if bulk and not empty:
            print(f"[info] {len(fresh)} new ids at once (> {NEW_CARD_MAX_BATCH}): "
                  "treated as a bulk update, not muted")
        elif not empty:
            print(f"[info] {len(fresh)} new card id(s) registered; muted for {NEW_CARD_DAYS:g} days")


def new_card_ids(db, now: float) -> set[str]:
    if NEW_CARD_DAYS <= 0:
        return set()
    return {r[0] for r in db.execute(
        "select card_id from seen where first_ts > ?", (now - NEW_CARD_DAYS * 86400,))}


# A card already alerted as a spike stays quiet this long unless it rises further.
SPIKE_COOLDOWN_HOURS = float(os.environ.get("FC_SPIKE_COOLDOWN_HOURS", "6"))
SPIKE_REALERT_PCT = 5.0          # ...or climbs this much above the price last alerted


# ---- round trips: a dip followed by a spike inside the last 24 h -------------
CYCLE_HOURS = 24                 # window searched for buy-low / sell-high pairs
CYCLE_RECENT_HOURS = 4           # the sell peak must be this fresh
CYCLE_MIN_NET_PCT = float(os.environ.get("FC_CYCLE_MIN_NET_PCT", "50"))  # profit after 5% EA tax
EA_TAX = 0.05
# Standalone spike alerts are off by default (set FC_SPIKE_ALERTS=1 to bring back).
SPIKE_ALERTS = os.environ.get("FC_SPIKE_ALERTS", "0") == "1"


def find_cycle(db, card: Card, now: float):
    """Best buy-low -> sell-high pair (low first, then later high) in the window."""
    rows = db.execute(
        "select ts, price from prices where card_id=? and ts>=? order by ts",
        (card.card_id, now - CYCLE_HOURS * 3600)).fetchall()
    rows = trim_outliers(rows)
    if len(rows) < MIN_SAMPLES:
        return None
    low_ts, low = rows[0]
    best = None
    for ts, price in rows[1:]:
        if price < low:
            low_ts, low = ts, price
        elif low > 0 and ts >= now - CYCLE_RECENT_HOURS * 3600:   # only fresh peaks
            net = (price * (1 - EA_TAX) - low) / low * 100
            if best is None or net > best["net"]:
                best = {"net": net, "gross": (price - low) / low * 100,
                        "buy": low, "buy_ts": low_ts, "sell": price, "sell_ts": ts}
    if not best or best["net"] < CYCLE_MIN_NET_PCT:
        return None
    return best


def fresh_cycles(db, items, now: float):
    """Drop cycles already posted (same card within the cooldown) and record new ones."""
    out = []
    for c, cy in items:
        row = db.execute(
            "select price from alerts where card_id=? and kind='cycle' and ts>=? "
            "order by ts desc limit 1", (c.card_id, now - 12 * 3600)).fetchone()
        if row and cy["sell"] < row[0] * (1 + SPIKE_REALERT_PCT / 100):
            continue
        out.append((c, cy))
        db.execute("insert into alerts values (?,?,?,?)", (c.card_id, "cycle", now, cy["sell"]))
    db.commit()
    return out


def build_cycles(items) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: -i[1]["net"]))
    if not items:
        return []
    lines = [f"Dip → spike inside {CYCLE_HOURS} h · profit after 5% EA tax ≥{CYCLE_MIN_NET_PCT:.0f}%", ""]
    for c, cy in items:
        lines.append(f"🔁 `{cy['net']:+.1f}%` net · buy **{k(cy['buy'])}** → sell **{k(cy['sell'])}** "
                     f"{link(c)} {c.rating} {c.kind}")
        lines.append(f"low <t:{int(cy['buy_ts'])}:t> · high <t:{int(cy['sell_ts'])}:t> · now {k(c.price)}")
    e = {"title": f"🔁 Round trips (dip → spike) · {PLATFORM_LABEL}",
         "description": "\n".join(lines), "color": 0xF1C40F,
         "footer": {"text": f"{CYCLE_HOURS} h window · net of 5% tax · from price snapshots"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


# ---- auto-watchlist: cards that keep completing round trips -------------------
AUTO_WATCH_MIN_CYCLES = int(os.environ.get("FC_AUTO_WATCH_MIN_CYCLES", "2"))
AUTO_WATCH_WINDOW_DAYS = 7       # count round trips inside this window
AUTO_WATCH_DAYS = 14             # stay watched this long after the last qualifying trip
CYCLE_GAP_HOURS = 6              # two alerts closer than this count as one round trip


def auto_watch_update(db, cards, now: float) -> None:
    """Add cards that appeared in >= AUTO_WATCH_MIN_CYCLES separate round trips."""
    for c in cards:
        stamps = [r[0] for r in db.execute(
            "select ts from alerts where card_id=? and kind='cycle' and ts>=? order by ts",
            (c.card_id, now - AUTO_WATCH_WINDOW_DAYS * 86400))]
        trips, last = 0, None
        for t in stamps:
            if last is None or t - last >= CYCLE_GAP_HOURS * 3600:
                trips += 1
                last = t
        if trips >= AUTO_WATCH_MIN_CYCLES:
            db.execute("insert or replace into watch_auto values (?,?)", (c.card_id, now))
            print(f"[info] auto-watch: {c.name} ({trips} round trips in {AUTO_WATCH_WINDOW_DAYS}d)")
    db.commit()


def auto_watch_ids(db, now: float) -> set[str]:
    return {r[0] for r in db.execute(
        "select card_id from watch_auto where added_ts>=?", (now - AUTO_WATCH_DAYS * 86400,))}


def watch_dip(db, card: Card, now: float):
    """Dip rule for a watched card: 6 h dip (median ref) or micro-dip vs last scan."""
    best = None
    s = card_stats(db, card, now)
    if s and s["ref"] > 0:
        pct = (card.price - s["ref"]) / s["ref"] * 100
        if pct <= -DIP_MIN_PCT:
            best = (pct, f"vs {k(s['ref'])} {COMPARE_HOURS}h ago", s["ref"])
    f = card_flash_stats(db, card)
    if f and f["prev_price"] > 0:
        pct = (card.price - f["prev_price"]) / f["prev_price"] * 100
        if pct <= -FLASH_DIP_PCT and (best is None or pct < best[0]):
            best = (pct, f"vs {k(f['prev_price'])} last scan", f["prev_price"])
    if not best:
        return None
    row = db.execute(
        "select price from alerts where card_id=? and kind='watch' and ts>=? "
        "order by ts desc limit 1", (card.card_id, now - SPIKE_COOLDOWN_HOURS * 3600)).fetchone()
    if row and card.price > row[0] * (1 - SPIKE_REALERT_PCT / 100):
        return None                               # already posted this dip
    db.execute("insert into alerts values (?,?,?,?)", (card.card_id, "watch", now, card.price))
    db.commit()
    return {"pct": best[0], "why": best[1], "ref": best[2]}


def build_watch_dips(items, auto: set[str]) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: i[1]["pct"]))
    if not items:
        return []
    lines = []
    for c, d in items:
        tag = " 🔁" if c.card_id in auto else ""
        lines.append(f"## {k(c.price)} · {link(c)} · {c.kind}{tag}")
        lines.append(f"📉 `{d['pct']:+.1f}%` · {c.rating} · {d['why']}")
    lines += ["", "🔁 = auto-added after repeated round trips"]
    e = {"title": f"👁 Watchlist dips · {PLATFORM_LABEL}",
         "description": "\n".join(lines), "color": 0x9B59B6,
         "footer": {"text": "Watched cards post only when they dip again"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


def fresh_spikes(db, items, now: float, kind: str):
    """Keep only spike items not already alerted recently; record the kept ones."""
    out = []
    for item in items:
        c = item[0]
        row = db.execute(
            "select price from alerts where card_id=? and kind=? and ts>=? "
            "order by ts desc limit 1",
            (c.card_id, kind, now - SPIKE_COOLDOWN_HOURS * 3600)).fetchone()
        if row and c.price < row[0] * (1 + SPIKE_REALERT_PCT / 100):
            continue                              # same spike, already posted
        out.append(item)
        db.execute("insert into alerts values (?,?,?,?)", (c.card_id, kind, now, c.price))
    db.execute("delete from alerts where ts < ?", (now - 8 * 86400,))
    db.commit()
    return out


def save_snapshot(db, cards: list[Card], now: float) -> None:
    db.executemany("insert into prices values (?,?,?)",
                   [(c.card_id, now, c.price) for c in cards])
    compact_history(db, now)
    db.commit()


# Raw snapshots are kept RAW_HOURS; older data is squeezed into one median per
# card per hour and kept until RETENTION_HOURS (3 days).
RAW_HOURS = max(HISTORY_HOURS, PREMIUM_HISTORY_HOURS) + 2
RETENTION_HOURS = 72
OUTCOME_KEEP_DAYS = 30           # dip outcomes older than this are dropped


def compact_history(db, now: float) -> None:
    cut = int((now - RAW_HOURS * 3600) // 3600) * 3600        # whole hours only
    rows = db.execute("select card_id, ts, price from prices where ts < ?", (cut,)).fetchall()
    if rows:
        buckets: dict = {}
        for cid, ts, price in rows:
            buckets.setdefault((cid, int(ts // 3600) * 3600), []).append(price)
        db.executemany("insert or replace into prices_hourly values (?,?,?)",
                       [(cid, h, int(statistics.median(v))) for (cid, h), v in buckets.items()])
        db.execute("delete from prices where ts < ?", (cut,))
    db.execute("delete from prices_hourly where hour_ts < ?", (now - RETENTION_HOURS * 3600,))
    db.execute("delete from outcomes where ts < ?", (now - OUTCOME_KEEP_DAYS * 86400,))


def history_rows(db, card_id: str, since: float) -> list[tuple[float, int]]:
    """Raw snapshots plus hourly medians (stamped mid-hour) for older data, oldest first."""
    raw = db.execute("select ts, price from prices where card_id=? and ts>=? order by ts",
                     (card_id, since)).fetchall()
    first_raw = raw[0][0] if raw else float("inf")
    old = db.execute("select hour_ts+1800, price from prices_hourly where card_id=? "
                     "and hour_ts+1800>=? and hour_ts+1800<? order by hour_ts",
                     (card_id, since, first_raw)).fetchall()
    return old + raw


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


# Outlier trimming for the baseline: snapshots far from the window's median
# (stale/inflated listings) are ignored so they can't distort the average.
OUTLIER_FACTOR = 5.0             # drop prices more than 5x the window median (no lower bound)
OUTLIER_ABS_MAX = 15_000_000     # and anything above this absolute cap


def trim_outliers(rows: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Drop snapshots far above the window median (or above the cap)."""
    if len(rows) < 3:
        return rows
    med = statistics.median(p for _, p in rows)
    kept = [(t, p) for t, p in rows
            if p <= med * OUTLIER_FACTOR and p <= OUTLIER_ABS_MAX]
    return kept or rows


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
    # Reference = median of the hour just before the compare point, so one
    # blip snapshot can't set the baseline (falls back to the single snapshot).
    band = [p for t, p in rows
            if now - (COMPARE_HOURS + 1) * 3600 <= t <= now - COMPARE_HOURS * 3600]
    ref = statistics.median(band) if len(band) >= 3 else older[-1]
    rows = trim_outliers(rows)
    prices = [p for _, p in rows]
    sales = extract_recent_sales(rows)
    sales_avg = int(statistics.mean(sales)) if sales else int(statistics.median(prices))
    sales_count = max(0, len(sales) - 1)
    q = statistics.quantiles(prices, n=4)
    return {
        "ref": ref,                             # median price ~COMPARE_HOURS ago
        "usual": statistics.median(prices),
        "sales_avg": sales_avg,
        "sales_count": sales_count,
        "snapshots": len(rows),
        "low": q[0],
        "high": q[2],
        "high24": max(p for t, p in rows if t >= now - 24 * 3600),
    }


def card_premium_dip_stats(db, card: Card, now: float, mkt_adj: float = 0.0) -> dict | None:
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

    rows = trim_outliers(rows)                   # ignore stale / inflated snapshots
    prices = [p for _, p in rows]
    sales = extract_recent_sales(rows)
    sales_count = max(0, len(sales) - 1)

    # ── liquidity / trading activity gate ───────────────────────────────────
    if sales_count < PREMIUM_MIN_PRICE_CHANGES:
        return None                              # price barely moved – illiquid

    # ── recent sales average ────────────────────────────────────────────────
    # Mean of the actual traded prices (filtering out stale idle periods)
    sales_avg = int(statistics.mean(sales))
    base = int(statistics.median(sales))         # median is the dip baseline
    if base <= 0:
        return None

    raw_pct = (card.price - base) / base * 100
    dip_pct = raw_pct - mkt_adj                  # dip beyond the market's own move
    if dip_pct > -PREMIUM_DIP_MIN_PCT:
        return None                              # not enough of a dip vs median

    q = statistics.quantiles(prices, n=4)
    return {
        "sales_avg": sales_avg,
        "sales_median": base,
        "usual": statistics.median(prices),
        "low": q[0],
        "high": q[2],
        "dip_pct": dip_pct,
        "raw_pct": raw_pct,
        "mkt": mkt_adj,
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
                             key=lambda i: _score_key(i[1], i[2])))
        if not band:
            continue
        lines = [f"**{k(lo)} – {k(hi)}**"]
        for c, s, pct in band:
            lines.append(f"## {k(c.price)} · {link(c)} · {c.kind}")
            lines.append(f"{tier_emoji(pct)} `{pct:+.1f}%` · {c.rating} · vs {k(s['ref'])} {COMPARE_HOURS}h ago")
            if s.get("score"):
                lines.append(f"↳ {score_text(s)}")
        lines += ["", "🔴 15%+ · 🟠 12–15% · 🟢 8–12%"]
        e = {"title": f"⚡ {COMPARE_HOURS} Hour dips {PLATFORM_LABEL}",
             "description": "\n".join(lines),
             "color": 0x2ECC71,
             "footer": {"text": f"{COMPARE_HOURS} Hour dips · recent avg price"}}
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
            lines.append(f"## ~~{k(f['prev_price'])}~~ → {k(c.price)} · {link(c)} · {c.kind}")
            lines.append(f"📉 `{pct:.1f}%` · {c.rating}"
                         + (f"\n↳ {score_text(f)}" if f.get("score") else ""))
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
            lines.append(f"## ~~{k(f['prev_price'])}~~ → {k(c.price)} · {link(c)} · {c.kind}")
            lines.append(f"💥 `{pct:.1f}%` · {c.rating}"
                         + (f"\n↳ {score_text(f)}" if f.get("score") else ""))
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

    Each entry already passed the 24 h median + liquidity gate in
    card_premium_dip_stats(), so everything shown here is a real signal.
    """
    items = _limit(sorted(items, key=lambda i: _score_key(i[1], i[1]["dip_pct"])))
    if not items:
        return []
    lines = [
        f"{len(items)} premium card(s) dipped ≥{PREMIUM_DIP_MIN_PCT:.0f}% vs 24 h median",
        f"Liquidity gate: ≥{PREMIUM_MIN_PRICE_CHANGES} price changes in last 24 h · {PREMIUM_HISTORY_HOURS}h window",
        "",
    ]
    for c, s in items:
        liq_bar = "🔥" * min(max(s["sales_count"] // 3, 1), 5)   # visual liquidity meter
        lines.append(f"## {k(c.price)} · {link(c)} · {c.kind}")
        lines.append(
            f"{tier_emoji(s['dip_pct'])} `{s['dip_pct']:+.1f}%` · {c.rating} · vs median {k(s['sales_median'])}"
        )
        if s.get("score"):
            lines.append(f"↳ {score_text(s)}")
    lines += ["", "🔴 15%+ · 🟠 12–15% · 🟢 8–12%"]
    e = {
        "title": f"💎 Premium Dips (Icon / Hero) · {PLATFORM_LABEL}",
        "description": "\n".join(lines),
        "color": 0x8E44AD,
        "footer": {"text": (
            f"24 h median · ≥{PREMIUM_DIP_MIN_PCT:.0f}% dip · "
            f"≥{PREMIUM_MIN_PRICE_CHANGES} price changes"
        )},
    }
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


def build_watchlist(cards_map: dict[str, Card], watchlist: set[str]) -> list[dict]:
    """Embed showing current price for every card on the watchlist."""
    hits = [cards_map[cid] for cid in watchlist if cid in cards_map]
    unlisted = [c for c in UNLISTED if c.card_id in watchlist]
    if not hits and not unlisted:
        return []
    hits = sorted(hits, key=lambda c: -c.price)
    lines = []
    for c in hits:
        lines.append(f"👁 **{k(c.price)}** {link(c)} {c.rating} {c.kind}")
    for c in unlisted:
        lines.append(f"👁 **no listing** {link(c)} {c.rating} {c.kind}")
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


# ---- market-wide move (peer group) -----------------------------------------
MARKET_MIN_GROUP = 8             # peers needed before a group move is subtracted


def rgroup(c: Card) -> str:
    base = "premium" if _is_premium(c) else "totw" if _is_totw(c) else "promo"
    return base + (" holo" if "holo" in c.kind.lower() else "")


def _group_median(vals: dict) -> dict:
    return {g: (statistics.median(v) if len(v) >= MARKET_MIN_GROUP else 0.0)
            for g, v in vals.items()}


def market_moves(db, cards, stats: dict, skip: set):
    """Median % move per peer group: vs 6 h ref, vs 24 h median, vs last scan."""
    v6, v24, vf = {}, {}, {}
    last = dict(db.execute("select card_id, price from prices "
                           "where ts=(select max(ts) from prices)").fetchall())
    for c in cards:
        if c.card_id in skip:
            continue
        g = rgroup(c)
        s = stats.get(c.card_id)
        if s and s["ref"] > 0:
            v6.setdefault(g, []).append((c.price - s["ref"]) / s["ref"] * 100)
        if s and s["usual"] > 0:
            v24.setdefault(g, []).append((c.price - s["usual"]) / s["usual"] * 100)
        lp = last.get(c.card_id)
        if lp:
            vf.setdefault(g, []).append((c.price - lp) / lp * 100)
    raw_f = {g: statistics.median(v) for g, v in vf.items() if len(v) >= MARKET_MIN_GROUP}
    return _group_median(v6), _group_median(v24), _group_median(vf), raw_f


# ---- seasonality: hour-of-week drift of each peer group ---------------------
SEASON_KEEP_DAYS = 56            # market-return history kept for the profile
SEASON_MIN_WEEKS = 3             # weeks of data before an hour-of-week bin is used
SEASON_MIN_DAYS = 7              # days of data before the time-of-day fallback is used
SEASON_SHRINK = 2.0              # bins with few observations are shrunk toward 0
SEASON_MIN_SCANS_PER_HOUR = 3    # an hour with fewer scans is ignored
SEASON_MAX_GAP = 2400            # seconds; longer gaps between scans aren't recorded


def record_market_returns(db, mret: dict, now: float) -> None:
    """Store each group's median % move since the previous scan."""
    db.executemany("insert into market_ret values (?,?,?)", [(g, now, r) for g, r in mret.items()])
    db.execute("delete from market_ret where ts < ?", (now - SEASON_KEEP_DAYS * 86400,))
    db.commit()


def season_profile(db, g: str, now: float):
    """Detrended mean hourly % move per hour-of-week (and time-of-day fallback), UTC."""
    rows = db.execute("select ts, ret from market_ret where g=? and ts>=?",
                      (g, now - SEASON_KEEP_DAYS * 86400)).fetchall()
    hours: dict = {}
    for ts, r in rows:
        h = int(ts // 3600)
        tot, n = hours.get(h, (0.0, 0))
        hours[h] = (tot + r, n + 1)
    hours = {h: t for h, (t, n) in hours.items() if n >= SEASON_MIN_SCANS_PER_HOUR}
    if not hours:
        return None
    overall = statistics.mean(hours.values())
    weekly: dict = {}
    daily: dict = {}
    for h, t in hours.items():
        how = time.gmtime(h * 3600)
        weekly.setdefault(how.tm_wday * 24 + how.tm_hour, []).append(t)
        daily.setdefault(how.tm_hour, []).append(t)

    def fit(bins):
        return {b: ((statistics.mean(v) - overall) * len(v) / (len(v) + SEASON_SHRINK), len(v))
                for b, v in bins.items()}
    return {"weekly": fit(weekly), "daily": fit(daily)}


def season_drift(db, g: str, now: float, cache: dict, horizon_h: int | None = None):
    """Expected extra % move of the group over the next horizon_h hours (None if unknown)."""
    horizon_h = horizon_h or SCORE_HORIZON
    if g not in cache:
        cache[g] = season_profile(db, g, now)
    prof = cache[g]
    if not prof:
        return None
    start = int(now // 3600)
    total, used = 0.0, False
    for j in range(horizon_h + 1):
        w = ((start + 1) * 3600 - now) / 3600 if j == 0 else 1.0
        if j == horizon_h:
            w = (now - start * 3600) / 3600 if horizon_h else w
        if w <= 0:
            continue
        t = time.gmtime((start + j) * 3600)
        wb = prof["weekly"].get(t.tm_wday * 24 + t.tm_hour)
        db_ = prof["daily"].get(t.tm_hour)
        if wb and wb[1] >= SEASON_MIN_WEEKS:
            total += w * wb[0]; used = True
        elif db_ and db_[1] >= SEASON_MIN_DAYS:
            total += w * db_[0]; used = True
    return total if used else None


def time_slot(ts: float) -> str:
    t = time.gmtime(ts)
    return ("wkend " if t.tm_wday >= 5 else "wkday ") + ("00-06", "06-12", "12-18", "18-24")[t.tm_hour // 6] + "Z"


# ---- dip outcomes + similar-dip score -------------------------------------
SCORE_HORIZON = 3                # hours: the return the score is based on
SCORE_MIN_N = int(os.environ.get("FC_SCORE_MIN_N", "20"))   # bucket size before backoff
SCORE_SHRINK_K = 15              # shrinkage strength toward the coarser bucket
OUTCOME_COOLDOWN_H = 6           # don't log the same card's dip again inside this
EXIT_HAIRCUT_PCT = 1.0           # exit below the floor (undercut) on top of the tax


def price_band(p: float) -> str:
    return ("<100k" if p < 100_000 else "100-500k" if p < 500_000
            else "500k-1.5M" if p < 1_500_000 else ">1.5M")


def depth_bin(d: float) -> str:
    d = abs(d)
    return "<10%" if d < 10 else "10-15%" if d < 15 else "15-25%" if d < 25 else "25%+"


def log_outcome(db, c: Card, dtype: str, rel: float, mkt: float, now: float) -> None:
    if db.execute("select 1 from outcomes where card_id=? and ts>=?",
                  (c.card_id, now - OUTCOME_COOLDOWN_H * 3600)).fetchone():
        return
    db.execute("insert into outcomes(card_id,kind,ts,price,depth,mkt,g,band,dbin,dtype,slot) "
               "values (?,?,?,?,?,?,?,?,?,?,?)",
               (c.card_id, c.kind, now, c.price, rel, mkt, rgroup(c),
                price_band(c.price), depth_bin(rel), dtype, time_slot(now)))


def resolve_outcomes(db, now: float) -> None:
    """Fill in the floor price 1/3/6 h after each logged dip (net of tax + haircut)."""
    rows = db.execute("select id, card_id, ts, price, r1, r3, r6 from outcomes where done=0").fetchall()
    for oid, cid, ts, price, *got in rows:
        vals = list(got)
        for i, h in enumerate((1, 3, 6)):
            target = ts + h * 3600
            if vals[i] is not None or now < target + 600:
                continue
            row = db.execute(
                "select price from prices where card_id=? and ts between ? and ? "
                "order by abs(ts-?) limit 1", (cid, target - 900, target + 1800, target)).fetchone()
            if row and price > 0:
                vals[i] = (row[0] * (1 - EA_TAX) - price) / price * 100 - EXIT_HAIRCUT_PCT
        done = 1 if (vals[2] is not None or now > ts + 8 * 3600) else 0
        db.execute("update outcomes set r1=?, r3=?, r6=?, done=? where id=?", (*vals, done, oid))
    db.commit()


def score_dip(db, g: str, band: str, dbin: str, dtype: str, slot: str = ""):
    """Shrunken average net return of similar past dips (nested buckets, coarse -> fine)."""
    col = {1: "r1", 3: "r3", 6: "r6"}[SCORE_HORIZON]
    rows = db.execute(f"select g, band, dbin, dtype, {col}, slot from outcomes "
                      f"where {col} is not null").fetchall()
    if len(rows) < SCORE_MIN_N:
        return {"status": "cold", "n": len(rows)}
    me = {"g": g, "band": band, "dbin": dbin, "dtype": dtype, "slot": slot}
    levels = [(), ("dbin",), ("g", "dbin"), ("g", "dbin", "dtype"),
              ("g", "band", "dbin", "dtype"), ("g", "band", "dbin", "dtype", "slot")]
    idx = {"g": 0, "band": 1, "dbin": 2, "dtype": 3, "slot": 5}
    parent, chosen = None, None
    for keys in levels:
        sel = [r[4] for r in rows if all(r[idx[kk]] == me[kk] for kk in keys)]
        if keys and len(sel) < SCORE_MIN_N:
            break
        n, tot = len(sel), sum(sel)
        est = tot / n if parent is None else (tot + SCORE_SHRINK_K * parent) / (n + SCORE_SHRINK_K)
        parent = est
        chosen = {"status": "ok", "mean": est, "n": n, "hits": sum(1 for x in sel if x > 0),
                  "label": " ".join(me[kk] for kk in keys) or "all dips"}
    return chosen


def score_text(d: dict) -> str:
    sc, season = d.get("score"), d.get("season")
    if not sc:
        return ""
    if sc["status"] == "cold":
        txt = f"similar dips: not enough history yet ({sc['n']}/{SCORE_MIN_N})"
    else:
        txt = (f"similar ({sc['label']}): {sc['hits']}/{sc['n']} profitable @{SCORE_HORIZON}h "
               f"· avg {sc['mean']:+.1f}% net")
    if season is not None:
        txt += f" · season {season:+.1f}%/{SCORE_HORIZON}h"
    return txt


def _score_key(d: dict, depth: float):
    sc = d.get("score")
    if sc and sc["status"] == "ok":
        return (0, -(sc["mean"] + (d.get("season") or 0.0)))   # expected net incl. seasonal drift
    return (1, depth)


def _mkt_text(d: dict) -> str:
    m = d.get("mkt", 0.0)
    return f" · mkt {m:+.1f}%" if abs(m) >= 0.5 else ""


def scan(db, cards: list[Card], now: float, watchlist: set[str] | None = None,
         spikes_on: bool = True, run_cycles: bool = True) -> list[dict]:
    dips, spikes, flash_spikes, flash_dips, flash_crashes = [], [], [], [], []
    cycles: list = []
    spikes_on = spikes_on and SPIKE_ALERTS
    premium_dips: list[tuple[Card, dict]] = []   # (card, stats) – own embed
    cards_map = {c.card_id: c for c in cards}
    muted_new = new_card_ids(db, now)
    watch = watchlist or set()
    auto = auto_watch_ids(db, now)
    watch_hits: list = []
    stats = {c.card_id: card_stats(db, c, now) for c in cards
             if c.card_id not in muted_new and c.card_id not in watch}
    prev_ts = db.execute("select max(ts) from prices").fetchone()[0]
    adj6, adj24, adjf, mret = market_moves(db, cards, stats, muted_new | watch)
    if prev_ts and now - prev_ts <= SEASON_MAX_GAP:       # feed the seasonal profile
        record_market_returns(db, mret, now)
    season_cache: dict = {}

    for c in cards:
        if c.card_id in watch:
            # watched cards skip the price/new-card filters and the normal lists;
            # they only post when they dip again.
            d = watch_dip(db, c, now)
            if d:
                watch_hits.append((c, d))
            cy = find_cycle(db, c, now)
            if cy:
                cycles.append((c, cy))
            continue
        if c.card_id in muted_new:
            continue                                  # released in the last few days
        # ── filter cards outside price range of interest ────────────────────
        if MIN_PRICE is not None and c.price < MIN_PRICE:
            continue
        if MAX_PRICE is not None and c.price > MAX_PRICE:
            continue

        # ── skip TOTW fodder at min price (noisy, no signal) ────────────────
        if _is_totw_fodder(c):
            continue

        cy = find_cycle(db, c, now)
        if cy:
            cycles.append((c, cy))

        if _is_premium(c):
            # ── premium path: dedicated dip detector (24 h, liquidity-gated) ─
            ps = card_premium_dip_stats(db, c, now, adj24.get(rgroup(c), 0.0))
            if ps is not None:
                premium_dips.append((c, ps))

            # Premium cards still participate in spike and flash alerts.
            # They are intentionally excluded from the general dip list to
            # avoid duplicate / lower-quality alerts for the same card.
            s = stats.get(c.card_id)
            if s and s["ref"] > 0:
                pct = (c.price - s["ref"]) / s["ref"] * 100
                if pct >= SPIKE_MIN_PCT:
                    spikes.append((c, s, pct))
        else:
            # ── standard path: 6h dip / spike ───────────────────────────────
            s = stats.get(c.card_id)
            if s and s["ref"] > 0:
                pct = (c.price - s["ref"]) / s["ref"] * 100
                m6 = adj6.get(rgroup(c), 0.0)
                rel = pct - m6                          # dip beyond the market's own move
                if rel <= -DIP_MIN_PCT and c.price < s["usual"] and c.price <= s["sales_avg"]:
                    dips.append((c, dict(s, mkt=m6, raw=pct), rel))
                if pct >= SPIKE_MIN_PCT:
                    spikes.append((c, s, pct))

        # ── micro-spike / dip / crash (last two snapshots) ───────────────────
        if c.price >= FLASH_MIN_PRICE:
            f = card_flash_stats(db, c)
            if f and f["prev_price"] > 0:
                pct = (c.price - f["prev_price"]) / f["prev_price"] * 100
                mf = adjf.get(rgroup(c), 0.0)
                rel = pct - mf
                f = dict(f, mkt=mf, raw=pct)
                if pct >= FLASH_SPIKE_PCT:
                    flash_spikes.append((c, f, pct))
                elif rel <= -FLASH_CRASH_PCT:
                    flash_crashes.append((c, f, rel))
                elif rel <= -FLASH_DIP_PCT:
                    flash_dips.append((c, f, rel))

    # score each dip by how similar past dips turned out, then log it for later scoring
    for items, dtype in ((dips, "6h"), (flash_dips, "micro"), (flash_crashes, "micro")):
        for c, d, rel in items:
            d["score"] = score_dip(db, rgroup(c), price_band(c.price), depth_bin(rel), dtype,
                                   time_slot(now))
            d["season"] = season_drift(db, rgroup(c), now, season_cache)
            log_outcome(db, c, dtype, rel, d.get("mkt", 0.0), now)
    for c, d in premium_dips:
        d["score"] = score_dip(db, rgroup(c), price_band(c.price), depth_bin(d["dip_pct"]), "prem",
                               time_slot(now))
        d["season"] = season_drift(db, rgroup(c), now, season_cache)
        log_outcome(db, c, "prem", d["dip_pct"], d.get("mkt", 0.0), now)
    db.commit()

    if spikes_on:
        spikes = fresh_spikes(db, spikes, now, "spike")
        flash_spikes = fresh_spikes(db, flash_spikes, now, "flash")
    _cyc = fresh_cycles(db, cycles, now) if run_cycles else []
    auto_watch_update(db, [c for c, _ in _cyc], now)
    embeds = (
        build_dips(dips)
        + (build_cycles(_cyc) if run_cycles else [])
        + (build_spikes(spikes) if spikes_on else [])
        + build_flashes(flash_spikes if spikes_on else [], flash_dips, flash_crashes)
        + build_premium_dips(premium_dips)    # 💎 Icon / Hero dips
    )

    # ── watchlist: posts only when a watched card dips again ─────────────────
    embeds += build_watch_dips(watch_hits, auto)

    return embeds


DISCORD_DESC_MAX = 3900          # Discord rejects embed descriptions over 4096 chars


def split_embed(e: dict) -> list[dict]:
    """Split an embed whose description is too long into several, on line breaks."""
    desc = e.get("description", "")
    if len(desc) <= DISCORD_DESC_MAX:
        return [e]
    chunks, cur = [], ""
    for line in desc.split("\n"):
        if cur and len(cur) + len(line) + 1 > DISCORD_DESC_MAX:
            chunks.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line[:DISCORD_DESC_MAX]
    if cur:
        chunks.append(cur)
    out = []
    for i, ch in enumerate(chunks):
        part = dict(e, description=ch)
        if i:
            part["title"] = f"{e.get('title', '')} (cont. {i + 1}/{len(chunks)})"[:256]
            part.pop("thumbnail", None)
        out.append(part)
    return out


def post(embeds: list[dict], dry_run: bool) -> None:
    flat = [part for e in embeds for part in split_embed(e)]
    for e in flat:
        if dry_run:
            print(json.dumps(e, ensure_ascii=False, indent=2))
            continue
        r = requests.post(WEBHOOK_URL, json={"embeds": [e]}, timeout=10)
        if r.status_code == 400:                      # show what Discord objected to
            print(f"[error] Discord 400 for '{e.get('title')}': {r.text[:300]} "
                  f"(description {len(e.get('description', ''))} chars)")
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


def run_cycle(db, dry_run: bool, watchlist: set[str] | None = None,
              spikes_on: bool = True) -> None:
    now = time.time()
    prev = db.execute("select max(ts) from prices").fetchone()[0]   # last saved scan
    try:
        watchlist = (watchlist or set()) | auto_watch_ids(db, now)   # manual + auto
        cards = fetch_market(watchlist)
        register_cards(db, _load_players().keys(), now)
        embeds = scan(db, cards, now, watchlist, spikes_on)   # judge against history first
        post(embeds, dry_run)
        save_snapshot(db, cards, now)
        resolve_outcomes(db, now)
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
    ap.add_argument("--runs", type=int, default=int(os.environ.get("FC_RUNS", "1")),
                    help="with --once: scan this many times in one launch (default 1)")
    ap.add_argument("--run-gap", type=float, default=float(os.environ.get("FC_RUN_GAP", "300")),
                    help="seconds between the scans of one launch (default 300, min 30)")
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
    watchlist = load_watchlist(
        args.watchlist or (WATCHLIST_FILE if os.path.exists(WATCHLIST_FILE) else None))
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
        if args.runs <= 1:
            run_cycle(db, args.dry_run, watchlist)   # errors fail the run, visible in Actions
            return
        if args.run_gap < 30:
            raise SystemExit("--run-gap must be at least 30 seconds")
        failed = 0
        for i in range(args.runs):
            if i:
                time.sleep(args.run_gap)
            print(f"[info] run {i + 1}/{args.runs}")
            try:
                # spike alerts (6h + micro) only on the first scan of a launch
                run_cycle(db, args.dry_run, watchlist, spikes_on=(i == 0))
            except Exception as e:
                failed += 1
                print(f"[warn] run {i + 1} failed: {e}")
        if failed == args.runs:
            raise SystemExit("every scan failed")
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
