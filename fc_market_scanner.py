"""
EA FC market scanner: finds price dips on special cards and posts them to a Discord webhook.

Alert types (one post per criterion; inside it cards are grouped by version: Icon, Hero, TOTW,
holo ...; each title names the period it compares against):
    📉 Dip Alert         · vs 6 h ago        regular cards, price down >=8% vs the 6 h reference
    ⚡ Fast Drop          · vs 5 min ago      down 8%+ against the last 5 minutes (💥 = 12%+)
    💎 Premium Dip        · vs 4-day sold     Icons/Heroes/pricey promo cards vs real sale prices
                                              (thin cards: vs 24 h median)
    🔁 Round Trip         · within 24 h       a card that dipped then recovered (flip profit)
    💰 Your Card Spiked   · vs your buy price held cards (purchased.json) up 15%+
    👁 Watchlist Dip      · vs 6 h / 5 min    watched cards dipping again
    👁 Watchlist Prices   · now               current price of every watched card
    🕒 Buy Windows        · next 24 h         cheapest and best-entry hours per card type, daily (FC_WINDOWS_POST_HOURS)
    📊 Market Index       · 24 h trend        whole-market index by segment, posted every 3 h (FC_INDEX_POST_HOURS)
    (🚀 Rise Alert and ⚡ Quick Spike exist but are off unless FC_SPIKE_ALERTS=1)

    fut.gg's data has no holo flag, so holo cards are labelled from holo_ids.json.
    Cards new to the list stay muted for FC_NEW_CARD_DAYS (4) days.

    pip install requests
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    export FC_PLATFORM=pc            # pc (default) or ps
    export FC_NOTIFY_EACH_RUN=1      # post a short "scan ran" line to Discord each run (0 = off)

    python fc_market_scanner.py --once --runs 13 --run-gap 60   # one launch (GitHub Actions)
    python fc_market_scanner.py --dry-run          # print embeds instead of posting
    python fc_market_scanner.py --demo --dry-run   # fake data, to preview the output
    python fc_market_scanner.py --interval 60 --max-scans 10 --db test.db --dry-run

How it works
  1. Each scan reads current prices from fut.gg's static price files (skipped when the feed
     has not changed) and labels them using players.json (see setup.py / build_players.py).
  2. Prices are saved to SQLite; sales_model.py infers real sales from floor-price moves.
  3. Dips, crashes, premium dips, round trips and held-card spikes are judged against the
     periods above and posted as Discord embeds.
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

import sales_model as sm      # sale inference from floor moves + fixed listing durations


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

DIP_MIN_PCT = 8.0
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
MAX_LINES = int(os.environ.get("FC_MAX_LINES", 16))   # per card-version group (0 = unlimited)

def _limit(items: list) -> list:
    """Limit the number of cards shown per embed/section, or return all if MAX_LINES <= 0."""
    return items[:MAX_LINES] if MAX_LINES > 0 else items

# ---- micro-spike / dip / crash (single scan-to-scan delta, ~15 min window) -
FLASH_SPIKE_PCT = 8.0            # jump between last two snapshots to count as micro-spike
FLASH_DIP_PCT = 8.0              # drop between last two snapshots to count as micro-dip
FLASH_CRASH_PCT = 12.0           # drop threshold that escalates a micro-dip to a crash
FLASH_MIN_PRICE = 10_000         # ignore cheap cards for flash alerts
FLASH_WINDOW_MIN = float(os.environ.get("FC_FLASH_WINDOW_MIN", "5"))   # reference = median price of the last N minutes

# ---- watchlist -------------------------------------------------------------
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json")   # auto-loaded if it exists
EMPTY_MARKET: list = []   # known cards with nothing listed right now (price 0)
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


def fetch_market(watchlist: set[str] | None = None, last_hash: str | None = None):
    """Current prices for tracked cards, from fut.gg's static price files."""
    manifest = _get_json(R2 + "manifest.json")      # maps names -> current hashes
    _cache["dyn_hash"] = manifest.get(f"player-prices-{PRICE_KEY}-dyn")
    if last_hash and _cache["dyn_hash"] == last_hash:
        return None                                  # fut.gg has not published new prices
    players = _load_players()
    ids = _load_ids(manifest, players)
    holo = _load_holo(players)
    prices = _r2(f"player-prices-{PRICE_KEY}-dyn", manifest)["p"]
    if len(prices) != len(ids):
        raise RuntimeError(f"{len(prices)} prices vs {len(ids)} ids - files out of sync")
    cards = []
    UNLISTED.clear()
    EMPTY_MARKET.clear()
    watch = watchlist or set()
    for ea_id, price in zip(ids, prices):
        meta = players.get(ea_id)
        if meta is None:
            continue                                 # unknown card
        watched = str(ea_id) in watch
        if price <= 0:
            EMPTY_MARKET.append(Card(str(ea_id), meta["commonName"], meta["overall"],
                                     meta["rarityName"] + (" 🃏 Holo" if ea_id in holo else ""),
                                     0, "", ""))     # nothing listed: lets the sales model see listings vanish
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
    db.execute("create table if not exists prices_daily(card_id text, day integer, open int, high int, "
               "low int, med int, close int, hours integer, moves integer, primary key(card_id, day))")
    db.execute("create table if not exists dip_events(id integer primary key autoincrement, card_id text, "
               "ts real, price int, usual int, dip real, mkt real, typ text, seg text, band text, "
               "persist int, fair int, sale_near int, wd int, hr int, x3 real, x6 real, x12 real)")
    db.execute("create index if not exists dx on dip_events(card_id, ts)")
    db.execute("create table if not exists index_ret(seg text, hour_ts integer, ret real, n integer, "
               "primary key(seg, hour_ts))")                    # permanent hourly index returns
    db.execute("create index if not exists ox on outcomes(card_id, ts)")
    sm.init(db)
    db.execute("create table if not exists meta(k text primary key, v text)")
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


# Dips: a card stays quiet this long after an alert unless it falls further.
DIP_COOLDOWN_MIN = float(os.environ.get("FC_DIP_COOLDOWN_MIN", "15"))
DIP_REALERT_PCT = 3.0            # ...or its price drops this much below the last alert


def fresh_dips(db, items, now: float, kind: str):
    out = []
    for item in items:
        c = item[0]
        row = db.execute(
            "select price from alerts where card_id=? and kind=? and ts>=? "
            "order by ts desc limit 1",
            (c.card_id, kind, now - DIP_COOLDOWN_MIN * 60)).fetchone()
        if row and c.price > row[0] * (1 - DIP_REALERT_PCT / 100):
            continue                              # same dip, already posted
        out.append(item)
        db.execute("insert into alerts values (?,?,?,?)", (c.card_id, kind, now, c.price))
    db.commit()
    return out


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
    lines = [f"⏱ Dip → recovery inside **{CYCLE_HOURS} h** · profit after 5% EA tax ≥{CYCLE_MIN_NET_PCT:.0f}%", ""]
    for c, cy in items:
        lines.append(f"🔁 `{cy['net']:+.1f}%` net · buy **{k(cy['buy'])}** → sell **{k(cy['sell'])}** "
                     f"{link(c)} {c.kind}")
        lines.append(f"low <t:{int(cy['buy_ts'])}:t> · high <t:{int(cy['sell_ts'])}:t> · now {k(c.price)}")
    e = {"title": f"🔁 Round Trip · within {CYCLE_HOURS} h · {PLATFORM_LABEL}",
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


WATCH_PREMIUM_CRASH_PCT = 50.0   # premium watchlist cards: alert on a drop this big within FLASH_WINDOW_MIN


def watch_dip(db, card: Card, now: float, premium: bool = False, mkt_adj: float = 0.0):
    """Dip rule for a watched card.

    Regular cards: 6 h dip (median ref) or quick dip vs the last 5 min.
    Premium cards: the premium rule (4-day sold price, or 24 h median when sales are thin),
    plus a crash alert when the price falls WATCH_PREMIUM_CRASH_PCT or more within 5 min.
    """
    best, kind, extra = None, "watch", ""
    if premium:
        ps = card_premium_dip_stats(db, card, now, mkt_adj)
        if ps:
            best = (ps["dip_pct"], f"vs {_premium_ref(ps)} {k(ps['sales_median'])}", ps["sales_median"])
            extra = f" · net {ps['net']:+.1f}%" if "net" in ps else ""
        flash_pct = WATCH_PREMIUM_CRASH_PCT
    else:
        s = card_stats(db, card, now)
        if s and s["ref"] > 0:
            pct = (card.price - s["ref"]) / s["ref"] * 100
            if pct <= -DIP_MIN_PCT:
                best = (pct, f"vs {k(s['ref'])} **{COMPARE_HOURS} h ago**", s["ref"])
        flash_pct = FLASH_DIP_PCT
    f = card_flash_stats(db, card, now)
    if f and f["prev_price"] > 0:
        pct = (card.price - f["prev_price"]) / f["prev_price"] * 100
        if pct <= -flash_pct and (best is None or pct < best[0]):
            best = (pct, f"vs {k(f['prev_price'])} **{FLASH_WINDOW_MIN:g} min ago**", f["prev_price"])
            extra = ""
            if premium:
                kind = "watchcrash"
    if not best:
        return None
    if kind == "watchcrash":                      # crash: short cooldown, re-alert on a further drop
        cool, realert = DIP_COOLDOWN_MIN * 60, DIP_REALERT_PCT
    else:
        cool, realert = SPIKE_COOLDOWN_HOURS * 3600, SPIKE_REALERT_PCT
    row = db.execute(
        "select price from alerts where card_id=? and kind=? and ts>=? "
        "order by ts desc limit 1", (card.card_id, kind, now - cool)).fetchone()
    if row and card.price > row[0] * (1 - realert / 100):
        return None                               # already posted this dip
    db.execute("insert into alerts values (?,?,?,?)", (card.card_id, kind, now, card.price))
    db.commit()
    return {"pct": best[0], "why": best[1], "ref": best[2], "extra": extra,
            "icon": "💥" if kind == "watchcrash" else "📉"}


def build_watch_dips(items, auto: set[str]) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: i[1]["pct"]))
    if not items:
        return []
    lines = [f"⏱ Regular cards: vs **{COMPARE_HOURS} h ago** · premium cards: vs **{PREMIUM_SALE_WINDOW_H // 24}-day sold** · "
             f"💥 premium crash ≥{WATCH_PREMIUM_CRASH_PCT:.0f}% vs **{FLASH_WINDOW_MIN:g} min ago**", ""]
    for c, d in items:
        tag = " 🔁" if c.card_id in auto else ""
        lines.append(f"### {k(c.price)} · {c.name} · {c.kind}{tag}")
        lines.append(f"{d.get('icon', '📉')} `{d['pct']:+.1f}%` · {d['why']}{d.get('extra', '')}{open_link(c)}")
    lines += ["", "🔁 = auto-added after repeated round trips"]
    e = {"title": f"👁 Watchlist Dip · {PLATFORM_LABEL}",
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


def get_meta(db, key: str):
    row = db.execute("select v from meta where k=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key: str, value) -> None:
    db.execute("insert or replace into meta values (?,?)", (key, str(value)))
    db.commit()


def save_snapshot(db, cards: list[Card], now: float) -> None:
    db.executemany("insert into prices values (?,?,?)",
                   [(c.card_id, now, c.price) for c in cards])
    compact_history(db, now)
    db.commit()


# Raw snapshots are kept RAW_HOURS; older data is squeezed into one median per card per hour
# and kept HOURLY_KEEP_DAYS (2 weeks); older hours are rolled up into one row per card per day
# (prices_daily) that is kept HISTORY_DAYS (8 weeks). The hourly market index per segment and per
# behaviour type (index_ret), the daily sales summaries and the dip-event ledger also keep 8 weeks.
RAW_HOURS = max(HISTORY_HOURS, PREMIUM_HISTORY_HOURS) + 2
HISTORY_DAYS = 56                # one history length (8 weeks) for every long-term table
HOURLY_KEEP_DAYS = 14            # per-card hourly prices; older hours roll up to daily rows
QUIET_HOURLY_KEEP_DAYS = 0       # Quiet cards barely move: hourly rows only for the current day, then daily rows
RETENTION_HOURS = HOURLY_KEEP_DAYS * 24
DAILY_KEEP_DAYS = HISTORY_DAYS
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
    roll_up_old_hours(db, now)
    db.execute("delete from outcomes where ts < ?", (now - OUTCOME_KEEP_DAYS * 86400,))


def roll_up_old_hours(db, now: float) -> None:
    """Hourly medians older than RETENTION_HOURS (Quiet cards: QUIET_HOURLY_KEEP_DAYS; whole UTC days) become one row per card per day:
    open, high, low, median, close, hours present and hour-to-hour price changes. Also squeezes
    old inferred sales. Runs at most once an hour."""
    last = get_meta(db, "rollup_ts")
    if last and now - float(last) < 3600:
        return
    cut = int((now - RETENTION_HOURS * 3600) // 86400) * 86400
    cut_quiet = int((now - QUIET_HOURLY_KEEP_DAYS * 86400) // 86400) * 86400
    try:
        quiet = {c for c, t in _behaviour_cached(db, now).items() if t == "Quiet"}
    except Exception:
        quiet = set()
    rows = db.execute("select card_id, hour_ts, price from prices_hourly where hour_ts < ? "
                      "order by card_id, hour_ts", (max(cut, cut_quiet),)).fetchall()
    by: dict = {}
    for cid, h, p in rows:
        if h < (cut_quiet if cid in quiet else cut):      # Quiet cards roll up to daily sooner
            by.setdefault((cid, int(h // 86400)), []).append(p)
    if by:
        db.executemany("insert or replace into prices_daily values (?,?,?,?,?,?,?,?,?)",
                       [(cid, day, v[0], max(v), min(v), int(statistics.median(v)), v[-1], len(v),
                         sum(1 for a, b in zip(v, v[1:]) if a != b)) for (cid, day), v in by.items()])
        db.execute("delete from prices_hourly where hour_ts < ?", (cut,))
        db.executemany("delete from prices_hourly where card_id=? and hour_ts < ?",
                       [(c, cut_quiet) for c in quiet])
    db.execute("delete from prices_daily where day < ?", (int((now - DAILY_KEEP_DAYS * 86400) // 86400),))
    try:                                          # hourly sale summaries for Mixed/Quiet cards: their floor
        beh = _behaviour_cached(db, now)          # hardly moves, so sales are the only sign of activity
        sm.roll_sales_hourly(db, now, [c for c, t in beh.items() if t in ("Mixed", "Quiet")])
    except Exception as e:
        print(f"[warn] hourly sales summary failed: {e}")
    sm.compact_sales(db, now)
    set_meta(db, "rollup_ts", now)


def history_rows(db, card_id: str, since: float) -> list[tuple[float, int]]:
    """Raw snapshots plus hourly medians (stamped mid-hour) for older data, oldest first."""
    raw = db.execute("select ts, price from prices where card_id=? and ts>=? order by ts",
                     (card_id, since)).fetchall()
    first_raw = raw[0][0] if raw else float("inf")
    old = db.execute("select hour_ts+1800, price from prices_hourly where card_id=? "
                     "and hour_ts+1800>=? and hour_ts+1800<? order by hour_ts",
                     (card_id, since, first_raw)).fetchall()
    return old + raw


def card_flash_stats(db, card: Card, now: float | None = None):
    """Reference price for micro-dip / crash detection: the median of the snapshots from the
    last FLASH_WINDOW_MIN minutes (or the newest one if it is older than that), so a crash
    that happens in two steps still adds up. scan() runs BEFORE the current prices are
    saved, so these rows are all earlier scans."""
    now = time.time() if now is None else now
    rows = db.execute(
        "select ts, price from prices where card_id=? and ts>=? order by ts desc",
        (card.card_id, now - FLASH_WINDOW_MIN * 60)).fetchall()
    if not rows:
        rows = db.execute(
            "select ts, price from prices where card_id=? order by ts desc limit 1",
            (card.card_id,)).fetchall()
    if not rows:
        return None
    return {"prev_price": statistics.median(p for _, p in rows), "prev_ts": rows[0][0]}


# ---- purchased list: spike alerts only for cards you hold ------------------------
PURCHASED_FILE = os.path.join(HERE, "purchased.json")
OWNED_SPIKE_PCT = float(os.environ.get("FC_OWNED_SPIKE_PCT", "15"))   # spike = this % above your buy price


PURCHASED_PROBLEMS: list[str] = []      # entries that matched no card / several cards
_ALIASES = {"icon": "base icon", "hero": "base hero", "totw": "team of the week",
            "dfg": "destined for glory", "fs": "future stars", "sp": "star performer",
            "sf": "squad foundations"}


def _norm(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode().lower()
    return " ".join(t.replace(",", " ").replace("|", " ").replace("-", " ").split())


def resolve_card(query: str) -> tuple[str | None, str]:
    """'Mbappe destined for glory', 'ronaldo icon holo', 'x totw 91' -> (card id, '') or
    (None, reason). Every word must appear in 'name promo [holo] rating'; 'holo' picks the
    holographic card, otherwise the normal one is preferred."""
    players, holo = _load_players(), _load_holo(_load_players())
    q = _norm(query)
    want_holo = any(w in ("holo", "holographic") for w in q.split())
    toks = []
    for w in q.split():
        if w in ("holo", "holographic"):
            continue
        toks += _ALIASES.get(w, w).split()
    if not toks:
        return None, "empty"
    hits = []
    for eid, row in players.items():
        hay = _norm(f"{row['commonName']} {row['cardName']} {row['rarityName']} {row['overall']}")
        if all(t in hay for t in toks):
            hits.append((eid, row, eid in holo or bool(row.get("holographicType"))))
    pool = [h for h in hits if h[2] == want_holo] or ([] if want_holo else hits)
    if len(pool) == 1:
        return str(pool[0][0]), ""
    if not pool:
        return None, "no card found" + (" (no holo version)" if want_holo and hits else "")
    pool.sort(key=lambda h: -h[1]["overall"])
    cands = ", ".join(f"{h[1]['commonName']} {h[1]['overall']} {h[1]['rarityName']}"
                      f"{' holo' if h[2] else ''}" for h in pool[:5])
    return None, f"{len(pool)} cards match, add promo/rating/holo: {cands}"


def load_purchased(path: str | None = None) -> dict[str, int | None]:
    """purchased.json: {"<card id or name + promo>": <buy price or null>, ...}, e.g.
    {"Mbappe destined for glory": "180k", "Ronaldo icon holo": null, "67146440": 150000}.
    Also accepts a plain list, or {"x": {"price": N}}. Returns {card id: price|None}."""
    path = path or PURCHASED_FILE
    PURCHASED_PROBLEMS.clear()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except ValueError as e:
        print(f"[warn] purchased.json is not valid JSON, ignored: {e}")
        PURCHASED_PROBLEMS.append(f"file is not valid JSON: {e}")
        return {}
    items = raw.items() if isinstance(raw, dict) else [(i, None) for i in raw]
    out: dict[str, int | None] = {}
    for key, v in items:
        if isinstance(v, dict):
            v = v.get("price")
        try:
            price = int(float(str(v).lower().replace("k", "e3").replace("m", "e6"))) if v else None
        except ValueError:
            price = None
        key = str(key).strip()
        if key.isdigit():
            cid = key
        else:
            cid, why = resolve_card(key)
            if not cid:
                PURCHASED_PROBLEMS.append(f"“{key}”: {why}")
                continue
        out[cid] = price
    print(f"[info] purchased list: {len(out)} card(s), {len(PURCHASED_PROBLEMS)} problem(s)")
    return out


def owned_spikes(db, cards_map: dict, purchased: dict, now: float) -> list:
    """Held cards whose price is OWNED_SPIKE_PCT or more above the price you paid."""
    out = []
    for cid, buy in purchased.items():
        c = cards_map.get(cid)
        if not c or c.price <= 0 or not buy:
            continue                                  # not listed, or no buy price given
        up = (c.price - buy) / buy * 100
        if up < OWNED_SPIKE_PCT:
            continue
        row = db.execute("select price from alerts where card_id=? and kind='owned' and ts>=? "
                         "order by ts desc limit 1",
                         (cid, now - float(DIP_COOLDOWN_MIN) * 60)).fetchone()
        if row and c.price < row[0] * (1 + DIP_REALERT_PCT / 100):
            continue
        db.execute("insert into alerts values (?,?,?,?)", (cid, "owned", now, c.price))
        net = (c.price * (1 - EA_TAX) - buy) / buy * 100
        out.append((c, {"buy": buy, "up": up, "net": net}))
    db.commit()
    return out


def build_owned_spikes(items) -> list[dict]:
    if not items:
        return []
    items = sorted(items, key=lambda i: -i[1]["up"])
    lines = ["⏱ Compared with the price you **bought** at", ""]
    for c, d in items:
        lines.append(f"### {k(c.price)} · {c.name} · {c.kind}")
        lines.append(f"🚀 `{d['up']:+.1f}%` · vs **bought** {k(d['buy'])} · net after tax "
                     f"`{d['net']:+.1f}%`{open_link(c)}")
    e = {"title": f"💰 Your Card Spiked · vs your buy price · {PLATFORM_LABEL}", "description": "\n".join(lines),
         "color": 0x2ECC71,
         "footer": {"text": f"Spike = {OWNED_SPIKE_PCT:.0f}%+ above your buy price · "
                            "net = after 5% EA tax at the current floor"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


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


# Premium dips judged on real (inferred) sales: fair value = recency-weighted median of the
# sold prices over PREMIUM_SALE_WINDOW_H.
PREMIUM_SALE_WINDOW_H = 96       # 4 days of sales
PREMIUM_HALFLIFE_H = 24          # newer sales count more
PREMIUM_MIN_SALES = 4            # sales in the window before this rule applies
PREMIUM_SOLD_MIN_DIP = 10.0      # % below fair value
PREMIUM_SIGMA_K = 2.0            # ...and at least this many sale-price spreads deep
PREMIUM_MIN_NET = 5.0            # % profit after tax if resold at fair value
PREMIUM_MIN_P3 = 0.25            # chance of a sale within 3 h (from the card's sale rate)


def _premium_from_sales(card, st: dict):
    fair = st["fair"]
    if fair <= 0:
        return None
    dip = (fair - card.price) / fair * 100
    if dip < max(PREMIUM_SOLD_MIN_DIP, PREMIUM_SIGMA_K * st["sigma_pct"]):
        return None
    if st["n"] >= 6 and (fair - st["last3"]) / fair * 100 >= 0.5 * dip:
        return None                                  # recent sales already fell: market sliding
    net = (fair * (1 - EA_TAX) - card.price) / card.price * 100
    if net < PREMIUM_MIN_NET or st["p3"] < PREMIUM_MIN_P3:
        return None
    return {"sales_avg": int(fair), "sales_median": int(fair), "base_kind": "sold",
            "usual": fair, "low": card.price, "high": fair, "dip_pct": -dip, "raw_pct": -dip,
            "mkt": 0.0, "sales_count": st["n24"], "snapshots": 0,
            "net": net, "p3": st["p3"], "sellp": st["p3"], "rank": net * st["p3"]}


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
    st = sm.sale_stats(db, card.card_id, now, PREMIUM_SALE_WINDOW_H, PREMIUM_HALFLIFE_H)
    if st and st["n"] >= PREMIUM_MIN_SALES:
        return _premium_from_sales(card, st)     # enough real sales: judge on them alone

    # fallback while sales are thin: floor-based rule over 24 h
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
    sold = None
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
        "base_kind": "sold" if sold else "median",
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


def _premium_ref(s: dict) -> str:
    """Comparison period of a premium dip, in bold: 4-day sold price or 24 h median."""
    if s.get("base_kind") == "sold":
        return f"**{PREMIUM_SALE_WINDOW_H // 24}-day sold**"
    return f"**{PREMIUM_HISTORY_HOURS} h median**"


def net_tier(net: float) -> str:
    return "🔴" if net >= 20 else "🟠" if net >= 10 else "🟢"


def link(c: Card) -> str:
    return f"[{c.name}]({c.url})" if c.url else f"**{c.name}**"


def open_link(c: Card) -> str:
    return f" · [open]({c.url})" if c.url else ""


def _version_order(kind: str):
    """Icons first, then Heroes, then other promos; normal before holo within a promo."""
    base = kind.replace(" 🃏 Holo", "")
    return (0 if "Icon" in base else 1 if "Hero" in base else 2, base, "Holo" in kind)


def by_version(items) -> list:
    """Group (card, ...) items by card version (promo + holo), in a fixed order."""
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it[0].kind, []).append(it)
    return [(kind, groups[kind]) for kind in sorted(groups, key=_version_order)]


def _group_header(kind: str, n: int) -> str:
    return f"**__{kind}__** · {n}"



def build_dips(items) -> list[dict]:
    """One post for the 6 h criterion, grouped by card version (Icon, Hero, TOTW, holo ...)."""
    if not items:
        return []
    lines = [f"⏱ Compared with the price **{COMPARE_HOURS} h ago**"]
    first = None
    for kind, grp in by_version(items):
        grp = _limit(sorted(grp, key=lambda i: _score_key(i[1], i[2])))
        lines += ["", _group_header(kind, len(grp))]
        for c, s, pct in grp:
            first = first or c
            lines.append(f"### {k(c.price)} · {c.name} · {c.kind}")
            lines.append(f"{tier_emoji(pct)} `{pct:+.1f}%` · vs {k(s['ref'])} **{COMPARE_HOURS} h ago**{_sell_text(s)}{open_link(c)}")
            if s.get("score"):
                lines.append(f"↳ {score_text(s)}")
    lines += ["", "🔴 15%+ · 🟠 12–15% · 🟢 8–12% below"]
    e = {"title": f"📉 Dip Alert · vs {COMPARE_HOURS} h ago · {PLATFORM_LABEL}",
         "description": "\n".join(lines),
         "color": 0x2ECC71,
         "footer": {"text": f"Dip Alert · compared with {COMPARE_HOURS} h ago"}}
    if first and first.image_url:
        e["thumbnail"] = {"url": first.image_url}
    return [e]

def build_spikes(items) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: -i[2]))
    if not items:
        return []
    lines = [f"⏱ {len(items)} cards up {SPIKE_MIN_PCT:.0f}%+ vs **{COMPARE_HOURS} h ago**", ""]
    for c, s, pct in items:
        verdict = ("well above usual – sell" if c.price > s["usual"] * 1.3
                   else "above usual – sell")
        lines.append(f"🔵 `▲{pct:.0f}%` {link(c)} {c.kind} · "
                     f"~~{k(s['ref'])}~~ → **{k(c.price)}**")
        lines.append(f"24h high {k(s['high24'])} · **{verdict}**")
    range_label = f" · {k(MIN_PRICE or 15_000)}–{k(MAX_PRICE) if MAX_PRICE else '∞'}"
    e = {"title": f"🚀 Rise Alert · vs {COMPARE_HOURS} h ago{range_label} · {PLATFORM_LABEL}",
         "description": "\n".join(lines),
         "color": 0xE67E22,
         "footer": {"text": f"Rise Alert · compared with {COMPARE_HOURS} h ago"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


def build_flashes(spike_items, dip_items, crash_items) -> list[dict]:
    """5-minute criterion: quick spikes, and quick dips + sharp crashes together, each grouped
    by card version. (Spikes are off unless FC_SPIKE_ALERTS=1.)"""
    embeds = []
    w = f"{FLASH_WINDOW_MIN:g} min ago"

    if spike_items:
        lines = [f"⏱ Up ≥{FLASH_SPIKE_PCT:.0f}% vs **{w}**"]
        for kind, grp in by_version(spike_items):
            grp = _limit(sorted(grp, key=lambda i: -i[2]))
            lines += ["", _group_header(kind, len(grp))]
            for c, f, pct in grp:
                lines.append(f"### ~~{k(f['prev_price'])}~~ → {k(c.price)} · {c.name} · {c.kind}")
                lines.append(f"⚡ `▲{pct:.1f}%` · vs **{w}**{open_link(c)}")
        e = {"title": f"⚡ Quick Spike · vs {w} · {PLATFORM_LABEL}", "description": "\n".join(lines),
             "color": 0xF1C40F, "footer": {"text": f"Quick Spike · compared with {w}"}}
        if spike_items[0][0].image_url:
            e["thumbnail"] = {"url": spike_items[0][0].image_url}
        embeds.append(e)

    drops = list(dip_items) + list(crash_items)
    if drops:
        lines = [f"⏱ Down ≥{FLASH_DIP_PCT:.0f}% vs **{w}**"]
        for kind, grp in by_version(drops):
            grp = _limit(sorted(grp, key=lambda i: i[2]))             # deepest first
            lines += ["", _group_header(kind, len(grp))]
            for c, f, pct in grp:
                icon = "💥" if pct <= -FLASH_CRASH_PCT else "📉"
                lines.append(f"### ~~{k(f['prev_price'])}~~ → {k(c.price)} · {c.name} · {c.kind}")
                lines.append(f"{icon} `{pct:.1f}%` · vs **{w}**{open_link(c)}"
                             + (f"\n↳ {score_text(f)}" if f.get("score") else ""))
        lines += ["", f"💥 {FLASH_CRASH_PCT:.0f}%+ · 📉 {FLASH_DIP_PCT:.0f}–{FLASH_CRASH_PCT:.0f}%"]
        e = {"title": f"⚡ Fast Drop · vs {w} · {PLATFORM_LABEL}", "description": "\n".join(lines),
             "color": 0xE74C3C if crash_items else 0x1ABC9C,
             "footer": {"text": f"Fast Drop · {FLASH_DIP_PCT:.0f}%+ below the price {w}"}}
        img = next((c.image_url for c, _, _ in drops if c.image_url), None)
        if img:
            e["thumbnail"] = {"url": img}
        embeds.append(e)
    return embeds

def build_premium_dips(items) -> list[dict]:
    """Premium-list dips (Icons/Heroes and pricey promo cards) against real sold prices,
    grouped by card version. Each entry already passed card_premium_dip_stats()."""
    if not items:
        return []
    lines = [
        f"⏱ Compared with **{PREMIUM_SALE_WINDOW_H // 24}-day sold prices**",
        f"(cards with under {PREMIUM_MIN_SALES} sales: vs **{PREMIUM_HISTORY_HOURS} h median** floor price)",
    ]
    first = None
    for kind, grp in by_version(items):
        grp = _limit(sorted(grp, key=lambda i: _score_key(i[1], i[1]["dip_pct"])))
        lines += ["", _group_header(kind, len(grp))]
        for c, s in grp:
            first = first or c
            lines.append(f"### {k(c.price)} · {c.name} · {c.kind}")
            lines.append(
                f"{net_tier(s['net']) if 'net' in s else tier_emoji(s['dip_pct'])} `{s['dip_pct']:+.1f}%` · "
                f"vs {_premium_ref(s)} {k(s['sales_median'])}"
                f"{' · net %+.1f%%' % s['net'] if 'net' in s else ''}"
                f"{_sell_text(s)}{open_link(c)}"
            )
            if s.get("score"):
                lines.append(f"↳ {score_text(s)}")
    legend = []
    if any("net" in x for _, x in items):
        legend.append("vs sold: 🔴 net 20%+ · 🟠 10–20% · 🟢 5–10%")
    if any("net" not in x for _, x in items):
        legend.append("vs median: 🔴 15%+ · 🟠 12–15% · 🟢 8–12% below")
    lines += [""] + legend
    e = {
        "title": f"💎 Premium Dip · vs {PREMIUM_SALE_WINDOW_H // 24}-day sold · {PLATFORM_LABEL}",
        "description": "\n".join(lines),
        "color": 0x8E44AD,
        "footer": {"text": (
            f"Net = profit after 5% tax if resold at fair value · "
            f"≥{PREMIUM_SOLD_MIN_DIP:.0f}% and ≥{PREMIUM_SIGMA_K:.0f} spreads deep"
        )},
    }
    if first and first.image_url:
        e["thumbnail"] = {"url": first.image_url}
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
    e = {"title": f"👁 Watchlist Prices · now · {PLATFORM_LABEL}",
         "description": "\n".join(lines),
         "color": 0x9B59B6,
         "footer": {"text": "Current floor price of every watched card"}}
    return [e]


# ---- market index: how the whole market is moving -------------------------------
INDEX_POST_HOURS = float(os.environ.get("FC_INDEX_POST_HOURS", "3"))   # 0 = never post the index
INDEX_HOURS = 24                     # the index is 100 at the start of this window
INDEX_MIN_PRICE = 10_000             # cards cheaper than this are too noisy for the basket
INDEX_MIN_CARDS = 5                  # a segment needs this many cards in a step to move
INDEX_SEGMENTS = ["All specials", "Icons", "Heroes", "TOTW", "Other promos", "Holos"]
INDEX_WINDOWS = [("1 h", 1), ("6 h", 6), ("12 h", 12), ("24 h", INDEX_HOURS)]


def _index_segment(kind: str) -> str:
    if "Holo" in kind:
        return "Holos"
    if "Icon" in kind:
        return "Icons"
    if "Hero" in kind:
        return "Heroes"
    if "Team of the week" in kind:
        return "TOTW"
    return "Other promos"


def market_index(db, now: float) -> dict:
    """Chain-linked, equal-weighted price index per segment, built from the saved history.

    Hourly price per card (median of its snapshots in that hour); each step the segment moves by
    the trimmed mean of its cards' price changes since the previous hour, so cards entering or
    leaving the basket never make the index jump. 100 = INDEX_HOURS hours ago.
    Returns {"series": {segment: [(hour_ts, value), ...]}, "cards": {segment: n}}.
    """
    since = now - INDEX_HOURS * 3600
    players, holo = _load_players(), _load_holo(_load_players())
    seg_of = {}
    for eid, row in players.items():
        kind = row["rarityName"] + (" 🃏 Holo" if eid in holo else "")
        seg_of[str(eid)] = _index_segment(kind)

    per: dict[str, dict[int, list]] = {}                 # card -> hour -> [prices]
    for cid, h, price in db.execute("select card_id, hour_ts, price from prices_hourly where hour_ts>=?",
                                    (since - 3600,)):
        per.setdefault(cid, {}).setdefault(int(h // 3600), []).append(price)
    raw: dict[str, dict[int, list]] = {}
    for cid, ts, price in db.execute("select card_id, ts, price from prices where ts>=?", (since - 3600,)):
        raw.setdefault(cid, {}).setdefault(int(ts // 3600), []).append(price)
    for cid, hrs in raw.items():                        # raw snapshots win over old hourly medians
        per.setdefault(cid, {}).update(hrs)
    px = {cid: {h: statistics.median(v) for h, v in hrs.items()} for cid, hrs in per.items()
          if cid in seg_of}

    hours = sorted({h for hrs in px.values() for h in hrs})
    series = {seg: [] for seg in INDEX_SEGMENTS}
    value = {seg: 100.0 for seg in INDEX_SEGMENTS}
    for i, h in enumerate(hours):
        if i:
            prev = hours[i - 1]
            rets = {seg: [] for seg in INDEX_SEGMENTS}
            for cid, hrs in px.items():
                a, b = hrs.get(prev), hrs.get(h)
                if a and b and a >= INDEX_MIN_PRICE and b >= INDEX_MIN_PRICE:
                    r = max(-0.4, min(0.4, b / a - 1))
                    rets["All specials"].append(r)
                    rets[seg_of[cid]].append(r)
            for seg, rs in rets.items():
                if len(rs) >= INDEX_MIN_CARDS:
                    rs.sort()
                    cut = len(rs) // 10                      # drop the top and bottom 10%
                    rs = rs[cut:len(rs) - cut] or rs
                    value[seg] *= 1 + statistics.mean(rs)
        for seg in INDEX_SEGMENTS:
            series[seg].append((h * 3600, value[seg]))
    cards = {seg: 0 for seg in INDEX_SEGMENTS}
    last = hours[-1] if hours else None
    for cid, hrs in px.items():
        if last in hrs and hrs[last] >= INDEX_MIN_PRICE:
            cards["All specials"] += 1
            cards[seg_of[cid]] += 1
    return {"series": series, "cards": cards}


def _index_change(pts: list, hours: float, now: float):
    """% change of the index over the last `hours`, or None if the history is too short."""
    target = now - hours * 3600
    old = [v for t, v in pts if t <= target]
    if not old or not pts:
        return None
    return (pts[-1][1] / old[-1] - 1) * 100


def _spark(vals: list, width: int = 16) -> str:
    if len(vals) < 2:
        return ""
    step = (len(vals) - 1) / (width - 1)
    pick = [vals[round(i * step)] for i in range(width)]
    lo, hi = min(pick), max(pick)
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[round((v - lo) / (hi - lo) * 7)] if hi > lo else bars[3] for v in pick)


def build_market_index(idx: dict, now: float) -> list[dict]:
    series, cards = idx["series"], idx["cards"]
    if not series["All specials"] or series["All specials"][-1][0] - series["All specials"][0][0] < 3 * 3600:
        return []                                         # not enough history yet
    head = f"{'':13}{'now':>6}" + "".join(f"{n:>8}" for n, _ in INDEX_WINDOWS) + f"  trend ({INDEX_HOURS} h)"
    rows = [head]
    for seg in INDEX_SEGMENTS:
        pts = series[seg]
        if not pts:
            continue
        cells = []
        for _, hrs in INDEX_WINDOWS:
            ch = _index_change(pts, hrs, now)
            cells.append(f"{'–':>8}" if ch is None else
                         f"{('▬' if round(ch, 1) == 0 else '▲' if ch > 0 else '▼') + f'{abs(ch):.1f}%':>8}")
        rows.append(f"{seg:13}{pts[-1][1]:6.1f}" + "".join(cells) + "  " + _spark([v for _, v in pts]))
    allpts = series["All specials"]
    ch6 = _index_change(allpts, 6, now)
    mood = "" if ch6 is None else (" 🟢 market rising" if ch6 > 0.5 else " 🔴 market falling" if ch6 < -0.5 else " ⚪ market flat")
    lines = [f"⏱ Index = 100 **{INDEX_HOURS} h ago** · changes over **1 h · 6 h · 12 h · 24 h**{mood}",
             "```", *rows, "```",
             "Basket: " + " · ".join(f"{seg} {cards[seg]}" for seg in INDEX_SEGMENTS if cards[seg])]
    return [{"title": f"📊 Market Index · {INDEX_HOURS} h trend · {PLATFORM_LABEL}",
             "description": "\n".join(lines), "color": 0x3498DB,
             "footer": {"text": "Equal-weighted basket of tracked special cards ≥10k, chained hourly · "
                                "mood = last 6 h"}}]


def maybe_post_index(db, now: float, dry_run: bool) -> None:
    """Post the market index every INDEX_POST_HOURS hours."""
    if INDEX_POST_HOURS <= 0:
        return
    last = get_meta(db, "index_post_ts")
    if last and now - float(last) < INDEX_POST_HOURS * 3600 - 120:
        return
    embeds = build_market_index(market_index(db, now), now)
    if embeds:
        post(embeds, dry_run)
        set_meta(db, "index_post_ts", now)


# ---- card behaviour types + weekly/daily rhythm -> buy windows ----------------------
BEHAVIOURS = ["Active", "Mixed", "Quiet"]
BEH_ACTIVE_MAX_STAY = 0.30           # floor unchanged in <=30% of updates -> Active
BEH_QUIET_MIN_STAY = 0.60            # floor unchanged in >=60% of updates -> Quiet
BEH_MIN_SNAPSHOTS = 40
RHYTHM_SEGMENTS = ["All specials"] + BEHAVIOURS
INDEX_RET_KEEP_DAYS = HISTORY_DAYS
WINDOWS_POST_HOURS = float(os.environ.get("FC_WINDOWS_POST_HOURS", "0"))   # 0 = never post (data is still collected)
RHYTHM_WEEKLY_MIN_DAYS = 21          # data span before the hour-of-week pattern is used
RHYTHM_MIN_OBS = 3                   # observations per hour bin


def card_behaviour(db, now: float) -> dict:
    """Active = floor changes almost every update (busy, thin pile); Quiet = floor holds
    (deep pile or little trading); Mixed in between. From the raw snapshots of the last 26 h."""
    out, cur, ps = {}, None, []

    def flush():
        if cur is not None and len(ps) >= BEH_MIN_SNAPSHOTS and statistics.median(ps) >= INDEX_MIN_PRICE:
            same = sum(1 for a, b in zip(ps, ps[1:]) if a == b) / (len(ps) - 1)
            out[cur] = ("Active" if same <= BEH_ACTIVE_MAX_STAY
                        else "Quiet" if same >= BEH_QUIET_MIN_STAY else "Mixed")
    for cid, price in db.execute("select card_id, price from prices where ts>=? order by card_id, ts",
                                 (now - RAW_HOURS * 3600,)):
        if cid != cur:
            flush()
            cur, ps = cid, []
        ps.append(price)
    flush()
    return out


def _hourly_px(db, since: float) -> dict:
    """card -> {hour number: median price in that hour}, from raw snapshots over hourly medians."""
    per: dict = {}
    for cid, h, price in db.execute("select card_id, hour_ts, price from prices_hourly where hour_ts>=?", (since,)):
        per.setdefault(cid, {}).setdefault(int(h // 3600), []).append(price)
    raw: dict = {}
    for cid, ts, price in db.execute("select card_id, ts, price from prices where ts>=?", (since,)):
        raw.setdefault(cid, {}).setdefault(int(ts // 3600), []).append(price)
    for cid, hrs in raw.items():
        per.setdefault(cid, {}).update(hrs)
    return {cid: {h: statistics.median(v) for h, v in hrs.items()} for cid, hrs in per.items()}


def record_index_hours(db, now: float) -> int:
    """Append the hourly return of every segment and behaviour type for each finished hour not
    recorded yet (the first call back-fills what the price history holds). Permanent, tiny."""
    cur_hour = int(now // 3600)
    last = db.execute("select max(hour_ts) from index_ret").fetchone()[0]
    first = int(last // 3600) + 1 if last else cur_hour - 118
    if first >= cur_hour:
        return 0
    players, holo = _load_players(), _load_holo(_load_players())
    seg_of = {str(eid): _index_segment(row["rarityName"] + (" 🃏 Holo" if eid in holo else ""))
              for eid, row in players.items()}
    beh = card_behaviour(db, now)
    px = {c: h for c, h in _hourly_px(db, (first - 1) * 3600).items() if c in seg_of}
    rows = []
    for h in range(first, cur_hour):
        prev = h - 1
        rets = {seg: [] for seg in INDEX_SEGMENTS + BEHAVIOURS}
        for cid, hrs in px.items():
            a, b = hrs.get(prev), hrs.get(h)
            if a and b and a >= INDEX_MIN_PRICE and b >= INDEX_MIN_PRICE:
                r = max(-0.4, min(0.4, b / a - 1))
                rets["All specials"].append(r)
                rets[seg_of[cid]].append(r)
                if cid in beh:
                    rets[beh[cid]].append(r)
        for seg, rs in rets.items():
            if len(rs) >= INDEX_MIN_CARDS:
                rs.sort()
                cut = len(rs) // 10
                rs = rs[cut:len(rs) - cut] or rs
                rows.append((seg, h * 3600, statistics.mean(rs), len(rs)))
    db.executemany("insert or ignore into index_ret values (?,?,?,?)", rows)
    db.execute("delete from index_ret where hour_ts < ?", (now - INDEX_RET_KEEP_DAYS * 86400,))
    db.commit()
    return len(rows)


def rhythm(db, seg: str, now: float):
    """Typical detrended % move per hour of the day (or of the week once there are 3 weeks),
    in Austin time. Returns None with too little history."""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    tz = ZoneInfo(TIMEZONE)
    rows = db.execute("select hour_ts, ret from index_ret where seg=? order by hour_ts", (seg,)).fetchall()
    if len(rows) < 48:
        return None
    span_days = (rows[-1][0] - rows[0][0]) / 86400
    overall = statistics.mean(r for _, r in rows)
    weekly = span_days >= RHYTHM_WEEKLY_MIN_DAYS
    bins: dict = {}
    for t, r in rows:
        d = _dt.datetime.fromtimestamp(t, tz)
        key = (d.weekday(), d.hour) if weekly else (0, d.hour)
        bins.setdefault(key, []).append((r - overall) * 100)
    if weekly and any(len(v) < RHYTHM_MIN_OBS for v in bins.values()):
        weekly = False                                    # fall back to the daily pattern
        bins = {}
        for t, r in rows:
            d = _dt.datetime.fromtimestamp(t, tz)
            bins.setdefault((0, d.hour), []).append((r - overall) * 100)
    mean = {k_: statistics.mean(v) for k_, v in bins.items()}
    se = {k_: (statistics.stdev(v) / len(v) ** 0.5 if len(v) > 1 else 9.9) for k_, v in bins.items()}
    n = {k_: len(v) for k_, v in bins.items()}
    return {"weekly": weekly, "mean": mean, "se": se, "n": n, "days": span_days}


def buy_windows(db, seg: str, now: float):
    """For the next 24 local hours: cheapest hour (lowest point of the typical cycle) and the
    best entry hour (largest typical gain over the following 3 h), with how sure the data is."""
    prof = rhythm(db, seg, now)
    if not prof:
        return None
    from zoneinfo import ZoneInfo
    import datetime as _dt
    tz = ZoneInfo(TIMEZONE)
    start = _dt.datetime.fromtimestamp(now, tz).replace(minute=0, second=0, microsecond=0)
    hours = [start + _dt.timedelta(hours=i) for i in range(1, 49)]      # 48 h so we can look ahead

    def key(d):
        return (d.weekday(), d.hour) if prof["weekly"] else (0, d.hour)
    m = [prof["mean"].get(key(d), 0.0) for d in hours]
    e = [prof["se"].get(key(d), 9.9) for d in hours]
    level, acc = [], 0.0
    for x in m[:24]:
        acc += x
        level.append(acc)
    low_i = min(range(24), key=lambda i: level[i])
    gains = [(sum(m[i + 1:i + 4]), (sum(x * x for x in e[i + 1:i + 4])) ** 0.5, i) for i in range(24)]
    g, se_g, gi = max(gains)
    return {"low": hours[low_i], "entry": hours[gi], "gain": g, "gain_se": se_g,
            "weekly": prof["weekly"], "days": prof["days"], "swing": max(level) - min(level)}


def _hr(d) -> str:
    return d.strftime("%a ") + d.strftime("%I %p").lstrip("0").lower()


def build_buy_windows(db, now: float) -> list[dict]:
    rows, conf = [], None
    for seg in RHYTHM_SEGMENTS:
        w = buy_windows(db, seg, now)
        if not w:
            continue
        conf = conf or w
        sure = "" if w["gain_se"] and abs(w["gain"]) >= 2.5 * w["gain_se"] else " (weak)"
        rows.append(f"{seg:13}{_hr(w['low']):>10}   {_hr(w['entry']):>10}  {w['gain']:+.2f}%{sure}")
    if not rows:
        return []
    beh = card_behaviour(db, now)
    counts = {b: sum(1 for v in beh.values() if v == b) for b in BEHAVIOURS}
    kind = (f"weekly pattern, {conf['days'] / 7:.1f} weeks of data" if conf["weekly"]
            else f"daily pattern only, {conf['days']:.1f} days of data - preliminary, the weekly pattern needs 3+ weeks")
    lines = [f"⏱ Next 24 h in **Austin time** · based on the {kind}", "```",
             f"{'':13}{'cheapest':>10}   {'best entry':>10}  gain over next 3 h", *rows, "```",
             "Active = floor moves almost every update (busy) · Quiet = floor holds (deep pile or little trading) · "
             "Mixed in between",
             "Cards: " + " · ".join(f"{b} {counts[b]}" for b in BEHAVIOURS)]
    return [{"title": f"🕒 Buy Windows · next 24 h · {PLATFORM_LABEL}", "description": "\n".join(lines),
             "color": 0x1ABC9C,
             "footer": {"text": "Typical hourly drift per card type, trend removed · a guide to timing, not a signal; "
                                "(weak) = within the noise"}}]


def maybe_post_buy_windows(db, now: float, dry_run: bool) -> None:
    if WINDOWS_POST_HOURS <= 0:
        return
    last = get_meta(db, "windows_post_ts")
    if last and now - float(last) < WINDOWS_POST_HOURS * 3600 - 120:
        return
    embeds = build_buy_windows(db, now)
    if embeds:
        post(embeds, dry_run)
        set_meta(db, "windows_post_ts", now)


# ---- dip-event ledger: every candidate dip + what happened next (the long record) ----------
EVENT_DIP_PCT = 8.0              # price this far below the card's 24 h median opens an event
EVENT_COOLDOWN_H = 6             # one event per card per episode
EVENT_MIN_PRICE = 10_000
EVENT_HORIZONS = (3, 6, 12)      # hours after the event at which the net result is measured
EVENT_KEEP_DAYS = HISTORY_DAYS


def _behaviour_cached(db, now: float) -> dict:
    hour = int(now // 3600)
    c = _cache.get("beh")
    if not c or c[0] != hour:
        _cache["beh"] = (hour, card_behaviour(db, now))
    return _cache["beh"][1]


def prune_active_sales(db, cards, now: float) -> int:
    """Active cards that are not Icons/Heroes keep inferred sales only as long as the raw floor
    snapshots (RAW_HOURS); their floor path already tells the story. Premium cards and Mixed/Quiet
    cards keep the full SALES_RAW_DAYS. Runs at most once an hour."""
    last = get_meta(db, "active_prune_ts")
    if last and now - float(last) < 3600:
        return 0
    beh = _behaviour_cached(db, now)
    ids = [c.card_id for c in cards if beh.get(c.card_id) == "Active" and not _is_premium(c)]
    n = 0
    cut = now - RAW_HOURS * 3600
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        n += db.execute(f"delete from sales_inferred where ts < ? and card_id in ({marks})",
                        (cut, *part)).rowcount
    db.commit()
    set_meta(db, "active_prune_ts", now)
    return n


def record_dip_events(db, cards, stats: dict, adj24: dict, skip: set, now: float) -> int:
    """Log each card that sits EVENT_DIP_PCT+ below its 24 h median, once per episode, with the
    situation at that moment (type, depth, market move, whether the dip persisted for two
    updates, sold fair value, a nearby inferred sale). Outcomes are filled in later."""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    beh = _behaviour_cached(db, now)
    prev_ts = db.execute("select max(ts) from prices").fetchone()[0]
    prev = dict(db.execute("select card_id, price from prices where ts=?", (prev_ts,))) if prev_ts else {}
    loc = _dt.datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    n = 0
    for c in cards:
        if c.card_id in skip or c.price < EVENT_MIN_PRICE:
            continue
        s_ = stats.get(c.card_id)
        if not s_ or s_["usual"] <= 0:
            continue
        dip = (c.price / s_["usual"] - 1) * 100
        if dip > -EVENT_DIP_PCT:
            continue
        persist = 1 if prev.get(c.card_id, 10 ** 12) <= s_["usual"] * (1 - EVENT_DIP_PCT / 100) else 0
        last = db.execute("select ts, persist from dip_events where card_id=? order by ts desc limit 1",
                          (c.card_id,)).fetchone()
        if last and now - last[0] < EVENT_COOLDOWN_H * 3600 and not (persist and not last[1]):
            continue                                   # same episode (unless it now persisted)
        st = sm.sale_stats(db, c.card_id, now, 96, 24)
        fair = int(st["fair"]) if st and st["n"] >= 4 else None
        near = db.execute("select count(*) from sales_inferred where card_id=? and kind='sale' "
                          "and ts>=? and price<=?", (c.card_id, now - 1800, c.price * 1.01)).fetchone()[0]
        db.execute("insert into dip_events(card_id, ts, price, usual, dip, mkt, typ, seg, band, persist, "
                   "fair, sale_near, wd, hr) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (c.card_id, now, c.price, int(s_["usual"]), dip, adj24.get(rgroup(c), 0.0),
                    beh.get(c.card_id, "?"), _index_segment(c.kind), price_band(c.price), persist,
                    fair, near, loc.weekday(), loc.hour))
        n += 1
    db.execute("delete from dip_events where ts < ?", (now - EVENT_KEEP_DAYS * 86400,))
    db.commit()
    return n


def resolve_dip_events(db, now: float) -> int:
    """Fill in the net result (after 5% tax and the exit haircut, in %) once each horizon has passed.
    999 marks a horizon whose price could not be found."""
    done = 0
    for hz in EVENT_HORIZONS:
        col = f"x{hz}"
        rows = db.execute(f"select id, card_id, ts, price from dip_events where {col} is null "
                          f"and ts + ? <= ?", (hz * 3600 + 1200, now)).fetchall()
        for eid, cid, ts, price in rows:
            t = ts + hz * 3600
            px = [r[0] for r in db.execute("select price from prices where card_id=? and ts between ? and ?",
                                           (cid, t - 1200, t + 1200))]
            if px:
                val = (statistics.median(px) * (1 - EA_TAX) / price - 1) * 100 - EXIT_HAIRCUT_PCT
            elif now - t > 2 * 3600:
                val = 999.0                                # no snapshot near that time, give up
            else:
                continue
            db.execute(f"update dip_events set {col}=? where id=?", (val, eid))
            done += 1
    db.commit()
    return done


# ------------------------------------------------------------------- scan --
def _is_premium(card: Card) -> bool:
    """True for Icons and Heroes (matched on words in the rarity name)."""
    kind = card.kind.lower()
    return any(word in kind for word in PREMIUM_WORDS)


# Premium list: Icons/Heroes (and their holos) above PREMIUM_ICON_HERO_MIN, plus any other
# special card (promo, TOTW ... and their holos) above PREMIUM_PROMO_MIN. These cards get the
# sale-based premium dip detector only and are left out of the other dip posts.
PREMIUM_ICON_HERO_MIN = parse_price(os.environ.get("FC_PREMIUM_ICON_HERO_MIN", 100_000))
PREMIUM_PROMO_MIN = parse_price(os.environ.get("FC_PREMIUM_PROMO_MIN", 500_000))


def _in_premium_list(card: Card, usual: float | None = None) -> bool:
    """Judged on the higher of the current price and its 24 h median, so a card that
    dips below the threshold stays in the premium list instead of dropping out of it."""
    floor = PREMIUM_ICON_HERO_MIN if _is_premium(card) else PREMIUM_PROMO_MIN
    return max(card.price, usual or 0) >= floor


def _is_totw(card: Card) -> bool:
    kind = card.kind.lower()
    return "team of the week" in kind or kind.startswith("totw")


def _is_totw_fodder(card: Card) -> bool:
    """True for TOTW cards below the minimum price threshold.

    These cards tend to sit at EA's minimum price and generate spurious
    min-price fluctuation alerts that add noise without actionable signal.
    """
    return _is_totw(card) and card.price < TOTW_MIN_PRICE


SOLD_MIN_N = 3                   # inferred sales needed before they replace the floor median


def _sell_text(d: dict) -> str:
    p = d.get("sellp")
    return f" · {p * 100:.0f}% sell ≤{SCORE_HORIZON}h" if p is not None else ""


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
SEASON_KEEP_DAYS = HISTORY_DAYS           # market-return history kept for the profile
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


TIMEZONE = os.environ.get("FC_TIMEZONE", "America/Chicago")   # Austin; shown in notices and logs


def local_stamp(ts: float | None = None) -> str:
    """Clock time like '01:47:02 CDT' in TIMEZONE (UTC if the zone data is missing)."""
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        return _dt.datetime.fromtimestamp(time.time() if ts is None else ts,
                                          ZoneInfo(TIMEZONE)).strftime("%H:%M:%S %Z")
    except Exception:
        return time.strftime("%H:%M:%S", time.gmtime(ts)) + " UTC"


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
    if d.get("rank") is not None:                         # sale-based premium dips
        return (-1, -d["rank"])
    sc = d.get("score")
    if sc and sc["status"] == "ok":
        return (0, -(sc["mean"] + (d.get("season") or 0.0)))   # expected net incl. seasonal drift
    return (1, depth)


def _mkt_text(d: dict) -> str:
    m = d.get("mkt", 0.0)
    return f" · mkt {m:+.1f}%" if abs(m) >= 0.5 else ""


def scan(db, cards: list[Card], now: float, watchlist: set[str] | None = None,
         spikes_on: bool = True, run_cycles: bool = True,
         purchased: dict | None = None) -> list[dict]:
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
    try:
        record_dip_events(db, cards, stats, adj24, muted_new | watch, now)   # long-term dip ledger
    except Exception as e:                                                    # never break a scan
        print(f"[warn] dip ledger failed: {e}")
    try:
        prune_active_sales(db, cards, now)
    except Exception as e:
        print(f"[warn] active sales prune failed: {e}")

    for c in cards:
        if c.card_id in watch:
            # watched cards skip the price/new-card filters and the normal lists;
            # they only post when they dip again.
            ws = card_stats(db, c, now)
            d = watch_dip(db, c, now, _in_premium_list(c, ws["usual"] if ws else None),
                          adj24.get(rgroup(c), 0.0))
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

        in_premium = _in_premium_list(c, (stats.get(c.card_id) or {}).get("usual"))
        if in_premium:
            # ── premium path: dedicated dip detector (24 h, liquidity-gated) ─
            ps = card_premium_dip_stats(db, c, now, adj24.get(rgroup(c), 0.0))
            if ps is not None:
                premium_dips.append((c, ps))

            # Premium cards still participate in spike alerts (flash alerts skip them).
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
        if c.price >= FLASH_MIN_PRICE and not in_premium:
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

    dips = fresh_dips(db, dips, now, "dip")
    flash_dips = fresh_dips(db, flash_dips, now, "flashdip")
    flash_crashes = fresh_dips(db, flash_crashes, now, "flashcrash")
    premium_dips = fresh_dips(db, premium_dips, now, "prem")

    # score each dip by how similar past dips turned out, then log it for later scoring
    for items, dtype in ((dips, "6h"), (flash_dips, "micro"), (flash_crashes, "micro")):
        for c, d, rel in items:
            d["score"] = score_dip(db, rgroup(c), price_band(c.price), depth_bin(rel), dtype,
                                   time_slot(now))
            d["season"] = season_drift(db, rgroup(c), now, season_cache)
            d["sellp"] = sm.sell_prob(db, rgroup(c), price_band(c.price), SCORE_HORIZON, now)
            log_outcome(db, c, dtype, rel, d.get("mkt", 0.0), now)
    for c, d in premium_dips:
        d["score"] = score_dip(db, rgroup(c), price_band(c.price), depth_bin(d["dip_pct"]), "prem",
                               time_slot(now))
        d["season"] = season_drift(db, rgroup(c), now, season_cache)
        if d.get("sellp") is None:
            d["sellp"] = sm.sell_prob(db, rgroup(c), price_band(c.price), SCORE_HORIZON, now)
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
        + build_premium_dips(premium_dips)    # 💎 premium dips
    )

    # ── watchlist: posts only when a watched card dips again ─────────────────
    embeds += build_watch_dips(watch_hits, auto)

    # ── purchased list: spikes only for cards you hold ───────────────────────
    if purchased:
        embeds += build_owned_spikes(owned_spikes(db, cards_map, purchased, now))

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
              spikes_on: bool = True, notify: bool = True) -> bool:
    """One scan. Returns False (and does nothing) if fut.gg has no new prices."""
    now = time.time()
    prev = db.execute("select max(ts) from prices").fetchone()[0]   # last saved scan
    try:
        watchlist = (watchlist or set()) | auto_watch_ids(db, now)   # manual + auto
        purchased = load_purchased()                  # re-read each scan: edits apply at once
        cards = fetch_market(watchlist | set(purchased), last_hash=get_meta(db, "dyn_hash"))
        if cards is None:
            print(f"[info] {local_stamp()} feed unchanged, skipped")
            return False
        register_cards(db, _load_players().keys(), now)
        embeds = scan(db, cards, now, watchlist, spikes_on, purchased=purchased)   # judge against history first
        sig = "\n".join(PURCHASED_PROBLEMS)                 # tell you once about unusable entries
        if sig != (get_meta(db, "purchased_warn") or ""):
            if sig:
                embeds.append({"title": "⚠️ purchased.json: fix these entries",
                               "description": "\n".join("• " + x for x in PURCHASED_PROBLEMS),
                               "color": 0xE67E22})
            set_meta(db, "purchased_warn", sig)
        post(embeds, dry_run)
        save_snapshot(db, cards, now)
        try:
            record_index_hours(db, now)                        # permanent hourly index record
            maybe_post_index(db, now, dry_run)                 # market index, every few hours
            maybe_post_buy_windows(db, now, dry_run)           # buy windows, once a day
        except Exception as e:                                 # never let the index break a scan
            print(f"[warn] market index failed: {e}")
        found = sm.update(db, [(c.card_id, c.price, rgroup(c), price_band(c.price)) for c in cards]
                          + [(c.card_id, 0, rgroup(c), None) for c in EMPTY_MARKET],
                          now, band_fn=price_band)
        if found:
            print(f"[info] inferred {found} listing(s) gone (sold or expired)")
        resolve_outcomes(db, now)
        try:
            resolve_dip_events(db, now)
        except Exception as e:
            print(f"[warn] dip ledger resolve failed: {e}")
        last_ts = get_meta(db, "dyn_ts")
        if last_ts:
            print(f"[info] feed updated {now - float(last_ts):.0f}s after the previous update")
        set_meta(db, "dyn_hash", _cache.get("dyn_hash"))
        set_meta(db, "dyn_ts", now)
    except Exception as e:
        notice(f"⚠️ {PLATFORM_LABEL} scan failed: {str(e)[:300]}", dry_run)
        raise
    stamp = local_stamp()
    print(f"[info] {stamp} scanned {len(cards)} cards, posted {len(embeds)} embed(s)")
    gap = f" · {fmt_gap(now - prev)} since last scan" if prev else " · first scan"
    via = os.environ.get("GITHUB_EVENT_NAME", "")      # set by GitHub Actions
    via = f" · via {via}" if via else ""
    if notify:
        notice(f"🔄 {PLATFORM_LABEL} scan ran at {stamp} · {len(cards)} cards · "
               f"{len(embeds)} alert embed(s){gap}{via}", dry_run)
    return True


def main() -> None:
    global MAX_LINES, MIN_PRICE, MAX_PRICE
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print, don't send")
    ap.add_argument("--once", action="store_true",
                    help="one scan then exit (for GitHub Actions / cron)")
    ap.add_argument("--runs", type=int, default=int(os.environ.get("FC_RUNS", "1")),
                    help="with --once: scan this many times in one launch (default 1)")
    ap.add_argument("--run-gap", type=float, default=float(os.environ.get("FC_RUN_GAP", "60")),
                    help="seconds between the scans of one launch (default 60, min 30)")
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
        first = True          # spike alerts and the status line: first executed scan only
        for i in range(args.runs):
            if i:
                time.sleep(args.run_gap)
            print(f"[info] run {i + 1}/{args.runs}")
            try:
                if run_cycle(db, args.dry_run, watchlist, spikes_on=first, notify=first):
                    first = False
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
