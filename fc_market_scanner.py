"""
EA FC market scanner: finds price dips on special cards and posts them to a Discord webhook.

Alert types (one post per criterion; inside it cards are grouped by version: Icon, Hero, TOTW,
holo ...; each title names the period it compares against):
    📉 Dip Alert         · vs 6 h ago        OFF by default (FC_DIP6H_ALERTS=1): regular cards, down >=8% vs 6 h ago
    ⚡ Fast Drop          · vs prev. update   down 10%+ against the previous price update (~5-6 min), with a tier hint
    💎 Premium Dip        · vs 4-day sold     Icons/Heroes/pricey promo cards vs real sale prices
                                              (thin cards: vs 24 h median)
    💰 Your Card Spiked   · vs your buy price held cards (purchased.json) up 15%+
    🎯 Watchlist Buy/Sell · vs 24 h median    watched cards buy signal & sell when target hit
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
  3. Dips, crashes, premium dips and held-card spikes are judged against the
     periods above and posted as Discord embeds.
"""
import argparse
import json
import math
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

DIP_MIN_PCT = 8.0                # drop vs COMPARE_HOURS ago
SPIKE_MIN_PCT = 8.0              # jump vs COMPARE_HOURS ago
MAX_LINES = int(os.environ.get("FC_MAX_LINES", 16))   # per card-version group (0 = unlimited)

def _limit(items: list) -> list:
    """Limit the number of cards shown per embed/section, or return all if MAX_LINES <= 0."""
    return items[:MAX_LINES] if MAX_LINES > 0 else items

# ---- micro-spike / dip / crash (single scan-to-scan delta, ~15 min window) -
FLASH_SPIKE_PCT = 8.0            # jump between last two snapshots to count as micro-spike
FLASH_DIP_PCT = float(os.environ.get("FC_FLASH_DIP_PCT", "10"))   # fast drop vs the last 5 minutes (8-10% drops did not pay after tax)
FAST_MIN_UNDER_SOLD = float(os.environ.get("FC_FAST_MIN_UNDER_SOLD", "5"))   # the new floor must be this % under the 4-day sold median
FAST_LIQUID_N24 = int(os.environ.get("FC_FAST_LIQUID_N24", "10"))           # liquid card: this many sales/24 h skips the sold-median filter
FAST_VOLUME_DIP_PCT = float(os.environ.get("FC_FAST_VOLUME_DIP_PCT", "8"))  # drop % that is enough on a volume-list card


# The volume list is fully dynamic: a card qualifies when ALL hold (re-checked hourly)
VOL_MIN_N24 = int(os.environ.get("FC_VOLUME_MIN_N24", "60"))        # inferred sales in the last 24 h
VOL_MIN_N96 = int(os.environ.get("FC_VOLUME_MIN_N96", "40"))        # ... and in 96 h (not a one-day burst)
VOL_MIN_PRICE = 12_000                                              # floor price range worth trading
VOL_MAX_PRICE = int(os.environ.get("FC_VOLUME_MAX_PRICE", "160000"))
VOL_MAX_SIGMA = float(os.environ.get("FC_VOLUME_MAX_SIGMA", "10"))  # sale-price noise (%): the sold median must mean something
VOL_KEEP_H = 48                                                     # stays on the list this long after last qualifying
_VOL_AUTO: set[str] = set()


def refresh_volume_cards(db, cards, now: float) -> None:
    """Hourly: rebuild the automatic part of the volume list from the sales model."""
    global _VOL_AUTO
    last = get_meta(db, "vol_auto_ts")
    cache = json.loads(get_meta(db, "vol_auto") or "{}")
    if not last or now - float(last) >= 3600:
        for c in cards:
            if not VOL_MIN_PRICE <= c.price <= VOL_MAX_PRICE:
                continue
            st = sm.sale_stats(db, c.card_id, now, 96, 24)
            if st and st["n24"] >= VOL_MIN_N24 and st["n"] >= VOL_MIN_N96 and st["sigma_pct"] <= VOL_MAX_SIGMA:
                cache[c.card_id] = now
        cache = {k_: t for k_, t in cache.items() if now - t < VOL_KEEP_H * 3600}
        set_meta(db, "vol_auto", json.dumps(cache))
        set_meta(db, "vol_auto_ts", now)
    _VOL_AUTO = set(cache)


def load_volume_list() -> set[str]:
    """The volume list is fully automatic: whatever refresh_volume_cards last selected."""
    return set(_VOL_AUTO)

FAST_MIN_SALES = 8                                                          # sales in 4 days needed to know that median
DIP6H_ALERTS = os.environ.get("FC_DIP6H_ALERTS", "0") == "1"      # the 6 h dip alert is off by default
FLASH_MIN_PRICE = 10_000         # ignore cheap cards for flash alerts
FLASH_WINDOW_MIN = float(os.environ.get("FC_FLASH_WINDOW_MIN", "1"))   # reference = median price of the last N minutes

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
FUTGG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.fut.gg",
    "Referer": "https://www.fut.gg/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
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
_session = None


def _get_session():
    global _session
    if _session is None:
        try:
            import importlib
            cffi_requests = importlib.import_module("curl_cffi.requests")
            _session = cffi_requests.Session(impersonate="chrome")
        except Exception:
            _session = requests.Session()
        _session.headers.update(FUTGG_HEADERS)
    return _session


def _get_json(url: str, retries: int = 3):
    s = _get_session()
    last_err = None
    for attempt in range(retries):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            r.raise_for_status()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Failed to fetch {url}")


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
    db.execute("create table if not exists prices_daily(card_id text, day integer, open int, high int, "
               "low int, med int, close int, hours integer, moves integer, primary key(card_id, day))")
    db.execute("create table if not exists dip_events(id integer primary key autoincrement, card_id text, "
               "ts real, price int, usual int, dip real, mkt real, typ text, seg text, band text, "
               "persist int, fair int, sale_near int, wd int, hr int, x3 real, x6 real, x12 real)")
    db.execute("create index if not exists dx on dip_events(card_id, ts)")
    have = {r[1] for r in db.execute("pragma table_info(dip_events)")}
    for col in ("xm15", "xm60", "best3", "d6"):             # quick-flip outcomes, and the 6 h-ago dip %, added later
        if col not in have:
            db.execute(f"alter table dip_events add column {col} real")
    for col, typ in (("verdict", "text"), ("v_h", "text"), ("v_n", "integer"), ("v_mean", "real"),
                     ("v_lb", "real"), ("v_win", "real"), ("v_lvl", "integer")):   # shadow scorer verdict
        if col not in have:
            db.execute(f"alter table dip_events add column {col} {typ}")
    db.execute("create table if not exists index_ret(seg text, hour_ts integer, ret real, n integer, "
               "primary key(seg, hour_ts))")                    # permanent hourly index returns
    db.execute("create index if not exists ox on outcomes(card_id, ts)")
    sm.init(db)
    db.execute("create table if not exists meta(k text primary key, v text)")
    db.execute("create table if not exists seen(card_id text primary key, first_ts real)")
    db.execute("create table if not exists watch_auto(card_id text primary key, added_ts real)")
    db.execute("create table if not exists watch_positions(card_id text primary key, buy_price int, "
               "target_price int, buy_ts real, est_hours real)")
    if "est_hours" not in {r[1] for r in db.execute("pragma table_info(watch_positions)")}:
        db.execute("alter table watch_positions add column est_hours real")
    if "warned" not in {r[1] for r in db.execute("pragma table_info(watch_positions)")}:
        db.execute("alter table watch_positions add column warned int default 0")
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


# Dip cooldown by behaviour type: busy cards can re-alert soon, quiet ones (and unclassified) wait longer.
DIP_COOLDOWN_BY_TYPE = {
    "Active": float(os.environ.get("FC_DIP_COOLDOWN_ACTIVE", "5")),
    "Mixed": float(os.environ.get("FC_DIP_COOLDOWN_MIXED", "10")),
    "Quiet": float(os.environ.get("FC_DIP_COOLDOWN_QUIET", str(DIP_COOLDOWN_MIN))),
}


def dip_cooldown_min(types: dict, card_id: str) -> float:
    return DIP_COOLDOWN_BY_TYPE.get(types.get(card_id), DIP_COOLDOWN_MIN)


# Premium-list and watched cards have no cooldown: they post again whenever the price has moved
# at least REPEAT_MOVE_PCT since the last post (up or down), or after REPEAT_SAME_MIN minutes if not.
REPEAT_MOVE_PCT = 1.0
REPEAT_SAME_MIN = 30


def repeat_blocked(db, card_id: str, kind: str, price: float, now: float) -> bool:
    row = db.execute("select price from alerts where card_id=? and kind=? and ts>=? order by ts desc limit 1",
                     (card_id, kind, now - REPEAT_SAME_MIN * 60)).fetchone()
    return bool(row and row[0] > 0 and abs(price / row[0] - 1) * 100 < REPEAT_MOVE_PCT)


def fresh_dips(db, items, now: float, kind: str):
    out = []
    try:
        types = _behaviour_cached(db, now)
    except Exception:
        types = {}
    for item in items:
        c = item[0]
        if kind == "prem":                        # premium list: no cooldown, only a price-change filter
            if repeat_blocked(db, c.card_id, kind, c.price, now):
                continue
            out.append(item)
            db.execute("insert into alerts values (?,?,?,?)", (c.card_id, kind, now, c.price))
            continue
        row = db.execute(
            "select price from alerts where card_id=? and kind=? and ts>=? "
            "order by ts desc limit 1",
            (c.card_id, kind, now - dip_cooldown_min(types, c.card_id) * 60)).fetchone()
        if row and c.price > row[0] * (1 - DIP_REALERT_PCT / 100):
            continue                              # same dip, already posted
        out.append(item)
        db.execute("insert into alerts values (?,?,?,?)", (c.card_id, kind, now, c.price))
    db.commit()
    return out


# A card already alerted as a spike stays quiet this long unless it rises further.
SPIKE_COOLDOWN_HOURS = float(os.environ.get("FC_SPIKE_COOLDOWN_HOURS", "6"))
SPIKE_REALERT_PCT = 5.0          # ...or climbs this much above the price last alerted


EA_TAX = 0.05
# Standalone spike alerts are off by default (set FC_SPIKE_ALERTS=1 to bring back).
SPIKE_ALERTS = os.environ.get("FC_SPIKE_ALERTS", "0") == "1"


# ---- auto-watchlist: liquid cards whose dips have paid back ------------------------------
# Every AUTO_REFRESH_H hours the last AUTO_LOOKBACK_DAYS of hourly prices are replayed for each
# liquid card: buy when the hourly price is AUTO_DIP_PCT below its trailing 24 h median (at most one
# buy per 6 h), sell 3, 6, 12 or 24 hours later, net of the 5% tax and the exit haircut.
# Cards whose dips pay (average above AUTO_MIN_MEAN %, wins in at least AUTO_MIN_WIN) at two or more holds
# are watched. New cards are left out (their prices are still finding a level).
AUTO_DIP_PCT = 6.0
AUTO_HOLDS = (3, 6, 12, 24)      # hold times tested, in hours
AUTO_MIN_HOLDS = 2               # a card must pay at this many of them (no single hold decides)
AUTO_GAP_H = 6                   # at most one replayed buy per card per this many hours
AUTO_MIN_EVENTS = int(os.environ.get("FC_AUTO_MIN_EVENTS", "3"))
AUTO_MIN_MEAN = 2.0
AUTO_MIN_WIN = 0.55
AUTO_MIN_SALES = 30              # inferred sales in 4 days (liquidity)
AUTO_MIN_PRICE = 30_000
AUTO_MAX = int(os.environ.get("FC_AUTO_MAX", "20"))
AUTO_LOOKBACK_DAYS = 7
AUTO_REFRESH_H = 6
AUTO_WATCH_DAYS = 3              # stay watched this long after last qualifying (avoids flicker)


def dip_replay(series: dict, thr_pct: float = AUTO_DIP_PCT, hold_h: int = 6) -> list[float]:
    """Net % (after tax and haircut) of each replayed dip-buy in one card's hourly price series."""
    out, last_h = [], -10 ** 9
    for h in sorted(series):
        if h - last_h < AUTO_GAP_H or series[h] < AUTO_MIN_PRICE:
            continue
        win = [series[x] for x in range(h - 24, h) if x in series]
        if len(win) < 12 or series[h] > statistics.median(win) * (1 - thr_pct / 100):
            continue
        if (h + hold_h) in series:
            out.append((series[h + hold_h] * (1 - EA_TAX) / series[h] - 1) * 100 - EXIT_HAIRCUT_PCT)
            last_h = h
    return out


def auto_watch_update(db, cards, now: float, skip: set | None = None) -> None:
    """Refresh the auto-watchlist (see above). Runs at most every AUTO_REFRESH_H hours."""
    last = get_meta(db, "auto_watch_ts")
    if not get_meta(db, "auto_watch_reset_v2"):          # one-time clean start: drop the round-trip list
        db.execute("delete from watch_auto")
        set_meta(db, "auto_watch_reset_v2", now)
        last = None
    if not get_meta(db, "auto_seed_v3"):                 # one-time: the starting list gets a grace period
        try:
            seed = json.load(open(os.path.join(HERE, "watch_seed.json")))
        except (OSError, ValueError):
            seed = []
        for cid in seed:
            db.execute("insert or replace into watch_auto values (?,?)", (str(cid), now))
        set_meta(db, "auto_seed_v3", now)
        last = None
    if last and now - float(last) < AUTO_REFRESH_H * 3600 - 120:
        return
    skip = skip or set()
    sales = dict(db.execute("select card_id, count(*) from sales_inferred where kind='sale' and ts>=? "
                            "group by card_id", (now - 96 * 3600,)))
    px = _hourly_px(db, now - AUTO_LOOKBACK_DAYS * 86400)
    picks = []
    for c in cards:
        if c.card_id in skip or c.price < AUTO_MIN_PRICE or sales.get(c.card_id, 0) < AUTO_MIN_SALES:
            continue
        ok = []
        for hold in AUTO_HOLDS:
            ev = dip_replay(px.get(c.card_id, {}), AUTO_DIP_PCT, hold)
            if len(ev) >= AUTO_MIN_EVENTS:
                mean, win = statistics.mean(ev), sum(1 for x in ev if x > 0) / len(ev)
                if mean > AUTO_MIN_MEAN and win >= AUTO_MIN_WIN:
                    ok.append((hold, len(ev), mean, win))
        if len(ok) >= AUTO_MIN_HOLDS:
            top = max(ok, key=lambda r: r[2] * r[3])
            picks.append((top[2] * top[3] * len(ok), c, top[1], top[2], top[3], top[0], len(ok)))
    picks.sort(key=lambda r: -r[0])
    for _, c, n, mean, win, hold, holds in picks[:AUTO_MAX]:
        db.execute("insert or replace into watch_auto values (?,?)", (c.card_id, now))
        print(f"[info] auto-watch: {c.name} ({holds} holds pay; best {hold}h: {n} dips, avg net {mean:+.1f}%, {win * 100:.0f}% won)")
    db.execute("delete from watch_auto where added_ts < ?", (now - AUTO_WATCH_DAYS * 86400,))
    set_meta(db, "auto_watch_ts", now)
    db.commit()


def auto_watch_ids(db, now: float) -> set[str]:
    return {r[0] for r in db.execute(
        "select card_id from watch_auto where added_ts>=?", (now - AUTO_WATCH_DAYS * 86400,))}


WATCH_BUY_MIN_NET = 4.0          # a watched card signals a buy when selling at fair value nets this much
WATCH_REASSESS_HOURS = float(os.environ.get("FC_WATCH_REASSESS_HOURS", "36"))   # target not reached by now: one reassess note
WATCH_POSITION_EXPIRE_HOURS = float(os.environ.get("FC_WATCH_EXPIRE_HOURS", "72"))


def target_line(price: float, fair: float, ref: str) -> str:
    """Where to list it: fair value, the net after the 5% EA tax, and the price that breaks even."""
    net = (fair * (1 - EA_TAX) / price - 1) * 100
    return (f"🎯 sell **{k(fair)}** ({ref}) · net {net:+.1f}% after tax · "
            f"break-even {k(price / (1 - EA_TAX))}")


def sell_target(db, card: Card, now: float, usual: float | None):
    """(fair value, label, net %) for a card: its 4-day sold median, else its 24 h median."""
    st = sm.sale_stats(db, card.card_id, now, PREMIUM_SALE_WINDOW_H, PREMIUM_HALFLIFE_H)
    if st and st["n"] >= 4:
        fair, ref = st["fair"], f"{PREMIUM_SALE_WINDOW_H // 24}-day sold"
    elif usual:
        fair, ref = usual, f"{PREMIUM_HISTORY_HOURS} h median"
    else:
        return None
    return fair, ref, (fair * (1 - EA_TAX) / card.price - 1) * 100


def estimate_sell_hours(db, card: Card, target_price: float, now: float) -> float:
    """Estimate how many hours it will take for a dipped card to reach target_price.

    Uses:
    1. Historical recovery time of past dips for this card in the last 7 days.
    2. Inferred sales velocity from the sales model (sales/hour to absorb floor listings).
    3. Default fallback of AUTO_GAP_H (6 hours).
    """
    try:
        since = now - AUTO_LOOKBACK_DAYS * 86400
        rows = db.execute(
            "select hour_ts, price from prices_hourly where card_id=? and hour_ts>=? order by hour_ts",
            (card.card_id, since)
        ).fetchall()
        if not rows:
            rows = db.execute(
                "select ts, price from prices where card_id=? and ts>=? order by ts",
                (card.card_id, since)
            ).fetchall()
        if len(rows) >= 6:
            recoveries = []
            dip_start = None
            for ts, price in rows:
                if dip_start is None:
                    if price <= target_price * (1 - AUTO_DIP_PCT / 100):
                        dip_start = ts
                else:
                    if price >= target_price:
                        recoveries.append((ts - dip_start) / 3600)
                        dip_start = None
            if recoveries:
                med_rec = statistics.median(recoveries)
                if med_rec > 0:
                    return float(round(max(1.0, min(24.0, med_rec)), 1))
    except Exception:
        pass

    try:
        st = sm.sale_stats(db, card.card_id, now, PREMIUM_SALE_WINDOW_H, PREMIUM_HALFLIFE_H)
        if st and st.get("n24", 0) >= 4:
            rate = st["n24"] / 24.0
            if rate > 0:
                est = 2.5 / rate
                return float(round(max(1.0, min(24.0, est)), 1))
    except Exception:
        pass

    return float(AUTO_GAP_H)


def watch_dip(db, card: Card, now: float):
    """Buy-signal rule for a watched card.

    Alerts only when the card is AUTO_DIP_PCT (6%) below its 24 h median AND
    selling at fair value (4-day sold or 24 h median) would net at least
    WATCH_BUY_MIN_NET (4%) after the 5% EA tax.  The watchlist has its own
    criterion — it does not reuse the 6 h / premium / flash dip rules.
    """
    ws = card_stats(db, card, now)
    usual = ws["usual"] if ws else None
    if not usual:
        return None
    tgt = sell_target(db, card, now, usual)
    if not tgt or card.price > usual * (1 - AUTO_DIP_PCT / 100) or tgt[2] < WATCH_BUY_MIN_NET:
        return None
    pct = (card.price - usual) / usual * 100
    kind = "watch"
    if repeat_blocked(db, card.card_id, kind, card.price, now):
        return None
    est_h = estimate_sell_hours(db, card, tgt[0], now)
    est_ts = now + est_h * 3600
    db.execute("insert into alerts values (?,?,?,?)", (card.card_id, kind, now, card.price))
    db.execute(
        "insert or replace into watch_positions(card_id, buy_price, target_price, buy_ts, est_hours) "
        "values (?,?,?,?,?)",
        (card.card_id, card.price, int(tgt[0]), now, est_h)
    )
    db.commit()
    return {"pct": pct, "why": f"vs {k(usual)} **{PREMIUM_HISTORY_HOURS} h median**", "ref": usual,
            "extra": f" · net {tgt[2]:+.1f}% at fair value",
            "icon": "🎯",
            "target": target_line(card.price, tgt[0], tgt[1]),
            "est_hours": est_h,
            "est_ts": est_ts,
            "sold": None}


def watch_sell(db, card: Card, now: float):
    """Sell-signal rule for a watched card with an active buy position.

    Fires when the card's price reaches or exceeds the target fair value established
    at buy time, completing the trading cycle.
    """
    row = db.execute(
        "select buy_price, target_price, buy_ts, est_hours, warned from watch_positions where card_id=?",
        (card.card_id,)
    ).fetchone()
    if not row:
        return None
    buy_price, target_price, buy_ts, est_hours, warned = row
    if card.price < target_price:
        held = (now - buy_ts) / 3600
        if held < WATCH_REASSESS_HOURS or warned:
            return None
        # target not reached in time: one reassess note, the position stays open until it expires
        db.execute("update watch_positions set warned=1 where card_id=?", (card.card_id,))
        db.commit()
        return {"stale": True, "buy_price": buy_price, "target_price": target_price,
                "net": (card.price * (1 - EA_TAX) - buy_price) / buy_price * 100,
                "held_h": held, "buy_ts": buy_ts, "est_hours": est_hours or 0.0}

    net = (card.price * (1 - EA_TAX) - buy_price) / buy_price * 100
    gross = (card.price - buy_price) / buy_price * 100
    held_h = max(0.1, (now - buy_ts) / 3600)

    db.execute("delete from watch_positions where card_id=?", (card.card_id,))
    db.execute("insert into alerts values (?,?,?,?)", (card.card_id, "watch_sell", now, card.price))
    db.commit()

    return {
        "buy_price": buy_price,
        "target_price": target_price,
        "net": net,
        "gross": gross,
        "held_h": held_h,
        "buy_ts": buy_ts,
        "est_hours": est_hours or 0.0,
    }


def build_watch_dips(items, auto: set[str]) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: (i[0].card_id in auto, i[1]["pct"])))   # your own picks first
    if not items:
        return []
    lines = [f"🎯 **{AUTO_DIP_PCT:.0f}%+** below 24 h median · fair value nets **{WATCH_BUY_MIN_NET:.0f}%+** after tax", ""]
    for c, d in items:
        tag = " 🔁" if c.card_id in auto else " ⭐"
        lines.append(f"### {k(c.price)} · {c.name} · {c.kind}{tag}")
        lines.append(f"{d.get('icon', '🎯')} `{d['pct']:+.1f}%` · {d['why']}{d.get('extra', '')}{open_link(c)}")
        if d.get("target"):
            lines.append(f"↳ {d['target']}")
        if d.get("est_ts"):
            eh = d["est_hours"]
            eh_str = f"{eh:.1f}h" if eh % 1 != 0 else f"{int(eh)}h"
            lines.append(f"↳ ⏱ est. sell <t:{int(d['est_ts'])}:t> (~{eh_str}, <t:{int(d['est_ts'])}:R>)")
        lines.append(name_block(c))
    lines += ["", "⭐ = on your watchlist · 🔁 = auto-watched"]
    e = {"title": f"🎯 Watchlist Buy Signal · {PLATFORM_LABEL}",
         "description": "\n".join(lines), "color": 0x9B59B6,
         "footer": {"text": f"Buy signal: {AUTO_DIP_PCT:.0f}%+ under 24 h median · "
                             f"fair value nets {WATCH_BUY_MIN_NET:.0f}%+ after 5% EA tax"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


def build_watch_sells(items, auto: set[str]) -> list[dict]:
    items = _limit(sorted(items, key=lambda i: (bool(i[1].get("stale")), i[0].card_id in auto, -i[1]["net"])))
    if not items:
        return []
    lines = ["🎯 **Target reached** · time to list and take profit  ·  ⏰ **Target not reached** · reassess", ""]
    for c, d in items:
        tag = " 🔁" if c.card_id in auto else " ⭐"
        lines.append(f"### {k(c.price)} · {c.name} · {c.kind}{tag}")
        if d.get("stale"):
            lines.append(f"⏰ `{d['net']:+.1f}%` net after tax if sold now · target **{k(d['target_price'])}** "
                         f"not reached in {int(round(d['held_h']))}h{open_link(c)}")
            lines.append(f"↳ bought at **{k(d['buy_price'])}** <t:{int(d['buy_ts'])}:R> · "
                         "consider exiting or lowering the target")
            continue
        lines.append(f"💰 `{d['net']:+.1f}%` net after tax · target **{k(d['target_price'])}** reached!{open_link(c)}")
        held_h = d["held_h"]
        held_str = f"{held_h:.1f}h" if held_h < 10 else f"{int(round(held_h))}h"
        est_str = f"~{int(round(d['est_hours']))}h" if d.get("est_hours") else ""
        timing = f"held ~{held_str}" + (f" (est. was {est_str})" if est_str else "")
        lines.append(f"↳ bought at **{k(d['buy_price'])}** <t:{int(d['buy_ts'])}:R> · {timing}")
    lines += ["", "⭐ = on your watchlist · 🔁 = auto-watched"]
    e = {"title": f"🎯 Watchlist Sell · {PLATFORM_LABEL}",
         "description": "\n".join(lines), "color": 0x2ECC71,
         "footer": {"text": "Watchlist cycle complete · net profit after 5% EA tax · target reached"}}
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
# and kept HOURLY_KEEP_DAYS (4 weeks); older hours are rolled up into one row per card per day
# (prices_daily) that is kept HISTORY_DAYS (8 weeks). The hourly market index per segment and per
# behaviour type (index_ret), the daily sales summaries and the dip-event ledger also keep 8 weeks.
RAW_HOURS = max(HISTORY_HOURS, PREMIUM_HISTORY_HOURS) + 2
HISTORY_DAYS = 56                # one history length (8 weeks) for every long-term table
HOURLY_KEEP_DAYS = 28            # per-card hourly prices (Active/Mixed); older hours roll up to daily rows
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
    try:                                          # hourly sale summaries, every card, built before the
        sm.roll_sales_hourly(db, now, None)       # raw rows age out (Active ones are trimmed after ~26 h)
    except Exception as e:
        print(f"[warn] hourly sales summary failed: {e}")
    sm.compact_sales(db, now)
    set_meta(db, "rollup_ts", now)


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
    return {"prev_price": statistics.median(p for _, p in rows), "prev_ts": rows[0][0],
            "age_min": (now - rows[0][0]) / 60}


# ---- purchased list: spike alerts only for cards you hold ------------------------
PURCHASED_FILE = os.path.join(HERE, "purchased.json")
OWNED_SPIKE_PCT = float(os.environ.get("FC_OWNED_SPIKE_PCT", "15"))   # spike = this % above your buy price


PURCHASED_TARGETS: dict[str, int] = {}      # optional per-card sell target from purchased.json
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
    PURCHASED_TARGETS.clear()
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
        target = None
        if isinstance(v, dict):
            target = v.get("target")
            v = v.get("price")
        try:
            price = int(float(str(v).lower().replace("k", "e3").replace("m", "e6"))) if v else None
            target = int(float(str(target).lower().replace("k", "e3").replace("m", "e6"))) if target else None
        except ValueError:
            price, target = None, None
        key = str(key).strip()
        if key.isdigit():
            cid = key
        else:
            cid, why = resolve_card(key)
            if not cid:
                PURCHASED_PROBLEMS.append(f"“{key}”: {why}")
                continue
        out[cid] = price
        if target:
            PURCHASED_TARGETS[cid] = target
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


OWNED_MIN_NET = 3.0              # default sell target must net at least this % over your buy price


def owned_sells(db, cards_map: dict, purchased: dict, now: float) -> list:
    """Held cards whose floor reached the sell target: the target in purchased.json, else the card's
    fair value (4-day sold / 24 h median), never below OWNED_MIN_NET % net over your buy price."""
    out = []
    for cid, buy in purchased.items():
        c = cards_map.get(cid)
        if not c or c.price <= 0 or not buy:
            continue
        target, ref = PURCHASED_TARGETS.get(cid), "your target"
        if not target:
            ws = card_stats(db, c, now)
            fair = sell_target(db, c, now, ws["usual"] if ws else None)
            floor_t = buy / (1 - EA_TAX) * (1 + OWNED_MIN_NET / 100)
            if fair and fair[0] >= floor_t:
                target, ref = fair[0], fair[1]
            else:
                target, ref = floor_t, f"+{OWNED_MIN_NET:.0f}% net"
        if c.price < target or repeat_blocked(db, cid, "owned_sell", c.price, now):
            continue
        db.execute("insert into alerts values (?,?,?,?)", (cid, "owned_sell", now, c.price))
        out.append((c, {"buy": buy, "target": target, "ref": ref,
                        "net": (c.price * (1 - EA_TAX) - buy) / buy * 100}))
    db.commit()
    return out


def build_owned_sells(items) -> list[dict]:
    if not items:
        return []
    items = sorted(items, key=lambda i: -i[1]["net"])
    lines = ["💰 The floor reached the sell target for a card you hold", ""]
    for c, d in items:
        lines.append(f"### {k(c.price)} · {c.name} · {c.kind}")
        lines.append(f"💰 `{d['net']:+.1f}%` net after tax · bought **{k(d['buy'])}** · "
                     f"target {k(d['target'])} ({d['ref']}){open_link(c)}")
    e = {"title": f"💰 Your Card · Sell Now · {PLATFORM_LABEL}", "description": "\n".join(lines),
         "color": 0x2ECC71,
         "footer": {"text": "Target: \"target\" in purchased.json, else fair value (min +3% net) · net = after 5% EA tax"}}
    if items[0][0].image_url:
        e["thumbnail"] = {"url": items[0][0].image_url}
    return [e]


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


def name_block(c: Card) -> str:
    """The player name in a code block: Discord shows a copy button on it (hold to copy on mobile),
    ready to paste into the Web App's Transfer Market search (which has no search link)."""
    return f"```\n{c.name}\n```"


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
        grp = _limit(sorted(grp, key=lambda i: i[2]))                      # deepest first
        lines += ["", _group_header(kind, len(grp))]
        for c, s, pct in grp:
            first = first or c
            lines.append(f"### {k(c.price)} · {c.name} · {c.kind}")
            lines.append(f"{tier_emoji(pct)} `{pct:+.1f}%` · vs {k(s['ref'])} **{COMPARE_HOURS} h ago**{_sell_text(s)}{open_link(c)}")
            lines.append(name_block(c))
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


def drop_tier(c: Card, beh: str | None, under: float, fair: float) -> str:
    """Hint line for a fast drop: how far the new floor sits under the 4-day sold median (🟢 8%+, 🟡 5-8%;
    past 5 s+ drops paid +27% net at 8%+ and about break-even under 5%), plus size, behaviour type, holo."""
    size = "<40k" if c.price < 40_000 else "40–100k" if c.price < 100_000 else "100k+"
    mark = "🟢" if under >= 8 else "🟡" if under >= FAST_MIN_UNDER_SOLD else "⚪"
    rel = f"{under:.0f}% under" if under >= 0 else f"{-under:.0f}% over"
    return (f"{mark} **{rel}** 4-day sold {k(fair)}{' · liquid, flip fast' if mark == '⚪' else ''} · {size} · {beh or 'type unknown'}"
            + (" · holo" if "Holo" in c.kind else ""))


def build_flashes(spike_items, dip_items) -> list[dict]:
    """5-minute criterion: quick spikes, and quick dips + sharp crashes together, each grouped
    by card version. (Spikes are off unless FC_SPIKE_ALERTS=1.)"""
    embeds = []
    w = "the previous update"        # fut.gg publishes prices about every 5-6 min, so this is the last snapshot

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

    def drop_embed(drops, vol: bool) -> dict:
        pct_need = FAST_VOLUME_DIP_PCT if vol else FLASH_DIP_PCT
        lines = [f"⏱ Down ≥{pct_need:.0f}% vs **{w}**"]
        for kind, grp in by_version(drops):
            grp = _limit(sorted(grp, key=lambda i: i[2]))             # deepest first
            lines += ["", _group_header(kind, len(grp))]
            for c, f, pct in grp:
                lines.append(f"### ~~{k(f['prev_price'])}~~ → {k(c.price)} · {c.name} · {c.kind}")
                ago = f" ({f['age_min']:.0f} min ago)" if f.get("age_min") else ""
                lines.append(f"📉 `{pct:.1f}%` · vs **{w}**{ago}{open_link(c)}"
                             + (f"\n↳ {f['tier']}" if f.get("tier") else ""))
                lines.append(name_block(c))
        if vol:
            lines += ["", "Volume list: high-turnover cards (automatic), no sold-median filter · "
                          "small margin, quick flip"]
            title, color, foot = f"📦 Volume Drop · vs {w} · {PLATFORM_LABEL}", 0x3498DB, \
                f"Volume Drop · {FAST_VOLUME_DIP_PCT:.0f}%+ below {w}"
        else:
            lines += ["", f"Only drops landing {FAST_MIN_UNDER_SOLD:.0f}%+ under the 4-day sold price are posted (cards with ≥{FAST_LIQUID_N24} sales/24 h excepted) · "
                          "🟢 8%+ under (best past results) · 🟡 5–8% under · ⚪ no sold filter"]
            title, color, foot = f"⚡ Fast Drop · vs {w} · {PLATFORM_LABEL}", 0x1ABC9C, \
                f"Fast Drop · {FLASH_DIP_PCT:.0f}%+ below {w} · {FAST_MIN_UNDER_SOLD:.0f}%+ under 4-day sold"
        e = {"title": title, "description": "\n".join(lines), "color": color, "footer": {"text": foot}}
        img = next((c.image_url for c, _, _ in drops if c.image_url), None)
        if img:
            e["thumbnail"] = {"url": img}
        return e

    drops = list(dip_items)
    general = [d for d in drops if not d[1].get("vol")]
    volume = [d for d in drops if d[1].get("vol")]
    if general:
        embeds.append(drop_embed(general, False))
    if volume:
        embeds.append(drop_embed(volume, True))
    return embeds

def build_premium_dips(items, hot: bool = False) -> list[dict]:
    """Premium-list dips (Icons/Heroes and pricey promo cards), grouped by card version.
    hot=True: the cards that fell below their 4-day SOLD price (the strongest signal) in their own
    red embed; hot=False: the rest, judged against the 24 h median floor while sales are thin."""
    items = [(c, s) for c, s in (items or []) if ("net" in s) == hot]
    if not items:
        return []
    if hot:
        lines = [f"⏱ Below the **{PREMIUM_SALE_WINDOW_H // 24}-day sold price** · "
                 f"≥{PREMIUM_SOLD_MIN_DIP:.0f}% and ≥{PREMIUM_SIGMA_K:.0f} spreads deep"]
    else:
        lines = [f"⏱ Under {PREMIUM_MIN_SALES} sales in {PREMIUM_SALE_WINDOW_H // 24} days · "
                 f"compared with the **{PREMIUM_HISTORY_HOURS} h median** floor price"]
    first = None
    for kind, grp in by_version(items):
        grp = _limit(sorted(grp, key=lambda i: -i[1]["rank"] if i[1].get("rank") is not None else i[1]["dip_pct"]))
        lines += ["", _group_header(kind, len(grp))]
        for c, s in grp:
            first = first or c
            head = f"{k(c.price)} · {c.name} · {c.kind}"
            lines.append(f"### 🚨 **{head}**" if hot else f"### {head}")
            lines.append(
                f"{net_tier(s['net']) if 'net' in s else tier_emoji(s['dip_pct'])} `{s['dip_pct']:+.1f}%` · "
                f"vs {_premium_ref(s)} {k(s['sales_median'])}"
                f"{' · net %+.1f%%' % s['net'] if 'net' in s else ''}"
                f"{_sell_text(s)}{open_link(c)}"
            )
            lines.append(f"↳ {target_line(c.price, s['sales_median'], _premium_ref(s).strip('*'))}")
            lines.append(name_block(c))
    if hot:
        lines += ["", "vs sold: 🔴 net 20%+ · 🟠 10–20% · 🟢 5–10%"]
    else:
        lines += ["", "vs median: 🔴 15%+ · 🟠 12–15% · 🟢 8–12% below"]
    e = {
        "title": (f"🚨💎 PREMIUM BELOW 4-DAY SOLD · {PLATFORM_LABEL}" if hot
                  else f"💎 Premium Dip · vs 24 h median · {PLATFORM_LABEL}"),
        "description": "\n".join(lines),
        "color": 0xE74C3C if hot else 0x8E44AD,
        "footer": {"text": ("Net = profit after 5% tax if resold at the sold price" if hot else
                            f"Fallback while a card has under {PREMIUM_MIN_SALES} sales · "
                            f"≥{PREMIUM_DIP_MIN_PCT:.0f}% below the median")},
    }
    if first and first.image_url:
        e["thumbnail"] = {"url": first.image_url}
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


# ---- 12-hour summary: what moved, how the watchlist and the alerts are doing ----------------
SUMMARY_HOURS = os.environ.get("FC_SUMMARY_HOURS", "7,19")          # Austin hours to post at ("" = never)
SUMMARY_WINDOW_H = 12
SUMMARY_MIN_SALES = 30           # inferred sales in 4 days for a card to count as liquid
SUMMARY_TOP = 5


def _med(vals):
    return statistics.median(vals) if vals else None


def summary_data(db, now: float, hours: int = SUMMARY_WINDOW_H) -> dict:
    """Numbers for the summary: segment moves, top risers/fallers, watchlist, alert scorecard, held cards.
    Moves compare each card's price now (median of the last 2 hourly values) with `hours` hours ago
    (median of the 3 hours around that point), so one stray listing cannot set either end."""
    players = _load_players()
    holo = _load_holo(players)
    meta = {str(k): (v["commonName"], v["rarityName"] + (" 🃏 Holo" if k in holo else ""), v["overall"])
            for k, v in players.items()}
    px = _hourly_px(db, now - (hours + 30) * 3600)
    cur = int(now // 3600)
    sales = dict(db.execute("select card_id, count(*) from sales_inferred where kind='sale' and ts>=? "
                            "group by card_id", (now - PREMIUM_SALE_WINDOW_H * 3600,)))
    try:
        muted = new_card_ids(db, now)
    except Exception:
        muted = set()
    latest = dict(db.execute("select card_id, price from prices where ts=(select max(ts) from prices)"))
    out = {"moves": [], "seg": {}, "latest": latest, "meta": meta, "hours": hours}
    for cid, s_ in px.items():
        if cid not in meta or cid not in latest:
            continue
        a = _med([s_[h] for h in (cur - hours - 1, cur - hours, cur - hours + 1) if h in s_])
        b = _med([s_[h] for h in (cur - 1, cur) if h in s_]) or latest[cid]
        if not a or not b or a < 15_000 or b < 15_000:
            continue
        out["moves"].append((cid, a, b, (b / a - 1) * 100, sales.get(cid, 0) >= SUMMARY_MIN_SALES, cid in muted))
    liquid = [m for m in out["moves"] if m[4] and not m[5]]
    segs = {"All specials": liquid}
    for m in liquid:
        segs.setdefault(_index_segment(meta[m[0]][1]), []).append(m)
    out["seg"] = {g: (statistics.median(x[3] for x in v), sum(1 for x in v if x[3] > 0) / len(v), len(v))
                  for g, v in segs.items() if len(v) >= 5}
    newm = [m for m in out["moves"] if m[5]]
    out["new"] = (statistics.median(x[3] for x in newm), len(newm)) if len(newm) >= 5 else None
    out["liquid"] = liquid
    return out


def _net_now(price_now: float, bought: float) -> float:
    return (price_now * (1 - EA_TAX) / bought - 1) * 100 - EXIT_HAIRCUT_PCT


def build_summary(db, now: float) -> list[dict]:
    d = summary_data(db, now)
    if not d["liquid"]:
        return []
    meta, latest, hours = d["meta"], d["latest"], d["hours"]
    move_of = {m[0]: m for m in d["moves"]}
    lines = [f"⏱ Last **{hours} h** · compared with each card's price {hours} h ago · liquid cards (≥{SUMMARY_MIN_SALES} sales in 4 days)", ""]
    # -- market by segment
    rows = [f"{'':13}{'median':>8} {'rising':>7} {'cards':>6}"]
    for g in ["All specials"] + [x for x in INDEX_SEGMENTS if x != "All specials"]:
        if g in d["seg"]:
            med, up, n = d["seg"][g]
            rows.append(f"{g:13}{('▲' if med > 0 else '▼' if med < 0 else '▬') + f'{abs(med):.1f}%':>8} {up * 100:6.0f}% {n:6d}")
    lines += ["**Market**", "```", *rows, "```"]
    if d["new"]:
        lines.append(f"New cards (muted): median {d['new'][0]:+.1f}% over {d['new'][1]} cards")
    # -- risers / fallers
    liq = sorted(d["liquid"], key=lambda m: m[3])

    def fmt(m):
        nm, kind, ov = meta[m[0]]
        return f"`{m[3]:+5.0f}%` {k(m[2])} · {nm} · {kind}"
    lines += ["", "**Rose most**"] + [fmt(m) for m in reversed(liq[-SUMMARY_TOP:])]
    lines += ["", "**Fell most**"] + [fmt(m) for m in liq[:SUMMARY_TOP]]
    # -- watchlist
    try:
        watch = load_watchlist(WATCHLIST_FILE) | auto_watch_ids(db, now)
    except Exception:
        watch = set()
    wl = []
    for cid in watch:
        m = move_of.get(cid)
        if not m or cid not in meta:
            continue
        wl.append((cid, m))
    if wl:
        lines += ["", f"**Watchlist** ({len(watch)} cards)"]
        pos = {r[0]: r for r in db.execute("select card_id, buy_price, target_price, buy_ts from watch_positions")}
        for cid, m in sorted(wl, key=lambda x: x[1][3]):
            nm, kind, ov = meta[cid]
            line = f"`{m[3]:+5.0f}%` {k(m[2])} · {nm} · {kind}"
            if cid in pos:
                _, buy, tgt, bts = pos[cid]
                line += f" · **open buy** {k(buy)} → target {k(tgt)} · {_net_now(latest.get(cid, m[2]), buy):+.1f}% net now"
            lines.append(line)
    # -- how the alerts did
    since = now - hours * 3600
    sc, first = {}, set()
    for cid, kind, ts, price in db.execute("select card_id, kind, ts, price from alerts where ts>=? and ts<=? "
                                           "order by ts", (since, now - 3600)):
        if cid in latest and price > 0 and kind in ("flashdip", "flashcrash", "prem", "watch", "dip") \
                and (cid, kind) not in first:             # first alert per card, so re-posts do not count twice
            first.add((cid, kind))
            sc.setdefault(kind, []).append(_net_now(latest[cid], price))
    if sc:
        names = {"flashdip": "⚡ Fast Drop", "flashcrash": "💥 Fast crash", "prem": "💎 Premium",
                 "watch": "🎯 Watchlist buy", "dip": "📉 6 h dip"}
        lines += ["", "**Alerts of the last 12 h, if sold now at the floor** (first alert per card, older than 1 h; net after 5% tax + 1% haircut)"]
        for kind, v in sc.items():
            lines.append(f"{names[kind]}: {len(v)} alerts · avg `{statistics.mean(v):+.1f}%` · median `{statistics.median(v):+.1f}%` · "
                         f"{sum(1 for x in v if x > 0) / len(v) * 100:.0f}% in profit")
    # -- cards you hold
    try:
        held = load_purchased()
    except Exception:
        held = {}
    hl = [(cid, b) for cid, b in held.items() if b and cid in latest and cid in meta]
    if hl:
        lines += ["", "**Cards you hold**"]
        for cid, b in hl:
            nm, kind, ov = meta[cid]
            tgt = PURCHASED_TARGETS.get(cid)
            lines.append(f"{nm} · {kind}: bought {k(b)} · floor {k(latest[cid])} · `{_net_now(latest[cid], b):+.1f}%` net if sold now"
                         + (f" · target {k(tgt)}" if tgt else ""))
    text = "\n".join(lines)
    return [{"title": f"🗞 {hours}-hour summary · {PLATFORM_LABEL}", "description": text[:DISCORD_DESC_MAX], "color": 0x95A5A6,
             "footer": {"text": "Medians of liquid cards ≥15k, unchained (each card vs its own price 12 h ago) · "
                                "alert results use today's floor, not what you could actually list at"}}]


# ---- bid sheet: sold-price reference per card, published for the userscript ------------
BID_SHEET_FILE = os.environ.get("FC_BID_SHEET", "bid_sheet.json")
BID_NETS = (3, 5, 8)             # target net profit % after tax + exit haircut
BID_MIN_SALES = 4                # fewer sales in 96 h: no reliable reference


def max_bid(sell: float, net_pct: float) -> int:
    """Highest buy price that still nets `net_pct` % when sold at `sell` (5% tax + exit haircut)."""
    return int(sell * (1 - EA_TAX) / (1 + (net_pct + EXIT_HAIRCUT_PCT) / 100))


def write_bid_sheet(db, cards, now: float) -> int:
    """bid_sheet.json: per card sold median, floor, sales rate and max bids. The workflow publishes it."""
    out = {}
    for c in cards:
        st = sm.sale_stats(db, c.card_id, now, 96, 24)
        if not st or st["n"] < BID_MIN_SALES:
            continue
        fair = int(st["fair"])
        out[c.card_id] = {"n": st["n"], "n24": st["n24"], "fair": fair, "floor": c.price,
                          "sig": round(st["sigma_pct"], 1),
                          "bid": [max_bid(fair, x) for x in BID_NETS]}
    if not out:
        return 0
    import json as _j
    tmp = BID_SHEET_FILE + ".tmp"
    with open(tmp, "w") as f:
        _j.dump({"ts": int(now), "nets": list(BID_NETS), "cards": out}, f, separators=(",", ":"))
    os.replace(tmp, BID_SHEET_FILE)
    return len(out)


def maybe_post_summary(db, now: float, dry_run: bool) -> None:
    """Post the summary at the Austin hours in FC_SUMMARY_HOURS (default 7 and 19), once per slot."""
    if not SUMMARY_HOURS.strip():
        return
    from zoneinfo import ZoneInfo
    import datetime as _dt
    loc = _dt.datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    hrs = {int(x) for x in SUMMARY_HOURS.split(",") if x.strip()}
    last = get_meta(db, "summary_post_ts")
    if loc.hour not in hrs or (last and now - float(last) < 10 * 3600):
        return
    embeds = build_summary(db, now)
    if embeds:
        post(embeds, dry_run)
        set_meta(db, "summary_post_ts", now)


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
# outcome columns: (column, seconds after the event, snapshot tolerance in seconds)
EVENT_HORIZONS = (("xm15", 900, 420), ("xm60", 3600, 600), ("x3", 3 * 3600, 1200),
                  ("x6", 6 * 3600, 1200), ("x12", 12 * 3600, 1200))
BEST_WINDOW_H = 3                # best3 = best exit price (15-minute medians) within this many hours
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
        mkt = adj24.get(rgroup(c), 0.0)
        typ = beh.get(c.card_id, "?")
        cur = db.execute("insert into dip_events(card_id, ts, price, usual, dip, mkt, typ, seg, band, persist, "
                         "fair, sale_near, wd, hr, d6) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (c.card_id, now, c.price, int(s_["usual"]), dip, mkt,
                          typ, _index_segment(c.kind), price_band(c.price), persist,
                          fair, near, loc.weekday(), loc.hour,
                          (c.price / s_["ref"] - 1) * 100 if s_.get("ref") else None))
        try:
            v = judge_dip(db, typ, dip, mkt, persist)       # shadow verdict, logged and never posted
            db.execute("update dip_events set verdict=?, v_h=?, v_n=?, v_mean=?, v_lb=?, v_win=?, v_lvl=? "
                       "where id=?", (v["verdict"], v["h"], v["n"], v["mean"], v["lb"], v["win"], v["lvl"],
                                      cur.lastrowid))
        except Exception as e:
            print(f"[warn] dip scorer failed: {e}")
        n += 1
    db.execute("delete from dip_events where ts < ?", (now - EVENT_KEEP_DAYS * 86400,))
    db.commit()
    return n


def _net_pct(sell: float, buy: float) -> float:
    """Net result in % of buying at `buy` and selling at `sell`, after 5% tax and the exit haircut."""
    return (sell * (1 - EA_TAX) / buy - 1) * 100 - EXIT_HAIRCUT_PCT


def resolve_dip_events(db, now: float) -> int:
    """Fill in each event's net result (after 5% tax and the exit haircut, in %) once the horizon
    has passed: 15 min, 1 h, 3 h, 6 h, 12 h, plus best3 = the best 15-minute median in the first
    3 hours (what a patient seller could have got). 999 marks a horizon whose price is missing."""
    done = 0
    for col, secs, tol in EVENT_HORIZONS:
        rows = db.execute(f"select id, card_id, ts, price from dip_events where {col} is null "
                          f"and ts + ? <= ?", (secs + tol, now)).fetchall()
        for eid, cid, ts, price in rows:
            t = ts + secs
            px = [r[0] for r in db.execute("select price from prices where card_id=? and ts between ? and ?",
                                           (cid, t - tol, t + tol))]
            if px:
                val = _net_pct(statistics.median(px), price)
            elif now - t > 2 * 3600:
                val = 999.0                                # no snapshot near that time, give up
            else:
                continue
            db.execute(f"update dip_events set {col}=? where id=?", (val, eid))
            done += 1
    span = BEST_WINDOW_H * 3600
    rows = db.execute("select id, card_id, ts, price from dip_events where best3 is null and ts + ? <= ?",
                      (span + 1200, now)).fetchall()
    for eid, cid, ts, price in rows:
        bins: dict = {}
        for t, p in db.execute("select ts, price from prices where card_id=? and ts > ? and ts <= ?",
                               (cid, ts, ts + span)):
            bins.setdefault(int((t - ts) // 900), []).append(p)
        if bins:
            val = _net_pct(max(statistics.median(v) for v in bins.values()), price)
        elif now - ts > span + 2 * 3600:
            val = 999.0
        else:
            continue
        db.execute("update dip_events set best3=? where id=?", (val, eid))
        done += 1
    db.commit()
    return done


# ---- dip scorer: judge a dip by what similar past dips actually returned ------------------
SCORER_MIN_N = int(os.environ.get("FC_SCORER_MIN_N", "30"))   # similar past events needed at a horizon
SCORER_MIN_WIN = 0.55            # share of similar events that ended in profit, for a BUY
SCORER_Z = 2.5                   # strict lower bound: the best of 4 horizons is picked, so a plain 95% would pass too much noise
SCORE_HORIZONS = (("xm15", "15 min"), ("xm60", "1 h"), ("x3", "3 h"), ("x6", "6 h"))
FALLING_MKT_PCT = -2.0           # the card's group is "falling" when it is down this much over 24 h


def _depth_bin(dip: float) -> str:
    a = abs(dip)
    return "8-12" if a < 12 else "12-20" if a < 20 else "20+"


def _depth_sql(b: str) -> tuple[float, float]:
    return {"8-12": (0.0, 12.0), "12-20": (12.0, 20.0), "20+": (20.0, 1e9)}[b]


def _winsor(v: list, lo_q: float = 0.05, hi_q: float = 0.95) -> list:
    if len(v) < 10:
        return v
    s = sorted(v)
    lo, hi = s[int(lo_q * (len(s) - 1))], s[int(hi_q * (len(s) - 1))]
    return [min(max(x, lo), hi) for x in v]


def _bound(vals: list) -> dict | None:
    """Mean of the winsorized net results, its one-sided 95% lower bound and the win rate."""
    vals = [x for x in vals if x is not None and x < 900]
    if len(vals) < 2:
        return None
    w = _winsor(vals)
    sd = statistics.pstdev(vals) or 1e-9               # spread of the raw results, outliers included
    m = statistics.mean(w)
    return {"n": len(vals), "mean": m, "lb": m - SCORER_Z * sd / math.sqrt(len(vals)),
            "win": sum(1 for x in vals if x > 0) / len(vals), "median": statistics.median(vals)}


def judge_dip(db, typ: str, dip: float, mkt: float, persist: int) -> dict:
    """Verdict for a dip from the past events most like it (same behaviour type, depth band,
    persistence, market direction). Looks for the most specific group that has SCORER_MIN_N resolved
    events at some horizon, then says:
      BUY      a horizon whose lower confidence bound is above zero net, with a win rate >= SCORER_MIN_WIN
      WATCH    similar dips were profitable on average but not reliably
      SKIP     similar dips lost on average
      LEARNING not enough similar past events yet."""
    lo, hi = _depth_sql(_depth_bin(dip))
    falling = 1 if mkt <= FALLING_MKT_PCT else 0
    levels = [
        ("typ=? and abs(dip)>=? and abs(dip)<? and persist=? and (mkt<=?)=?", (typ, lo, hi, persist, FALLING_MKT_PCT, falling)),
        ("typ=? and abs(dip)>=? and abs(dip)<? and persist=?", (typ, lo, hi, persist)),
        ("typ=? and abs(dip)>=? and abs(dip)<?", (typ, lo, hi)),
    ]                            # never mixes behaviour types: Active dips say nothing about Quiet ones
    for lvl, (where, args) in enumerate(levels):
        found = []
        for col, label in SCORE_HORIZONS:
            vals = [r[0] for r in db.execute(f"select {col} from dip_events where {where} and {col} is not null "
                                             f"and {col}<900", args)]
            st_ = _bound(vals)
            if st_ and st_["n"] >= SCORER_MIN_N:
                found.append((label, st_))
        if not found:
            continue
        top = max(found, key=lambda x: x[1]["lb"])
        verdict = ("BUY" if top[1]["lb"] > 0 and top[1]["win"] >= SCORER_MIN_WIN
                   else "WATCH" if max(f_[1]["mean"] for f_ in found) > 0 else "SKIP")
        return {"verdict": verdict, "h": top[0], "n": top[1]["n"], "mean": top[1]["mean"],
                "lb": top[1]["lb"], "win": top[1]["win"], "lvl": lvl}
    return {"verdict": "LEARNING", "h": None, "n": 0, "mean": None, "lb": None, "win": None, "lvl": None}


def scorer_report(db, now: float) -> list[dict]:
    """Scoreboard of the shadow verdicts: what each verdict group actually returned (net, after tax)."""
    rows = db.execute("select verdict, xm15, xm60, x3, best3 from dip_events where verdict is not null "
                      "and xm60 is not null and xm60 < 900").fetchall()
    if not rows:
        return []
    groups = {}
    for verdict, a, b, c, d in rows:
        for key in (verdict, "ALL dips"):
            groups.setdefault(key, []).append((a, b, c, d))

    def fmt(v):
        v = [x for x in v if x is not None and x < 900]
        if len(v) < 3:
            return "   –    "
        return f"{statistics.median(v):+5.1f}% {100 * sum(1 for x in v if x > 0) / len(v):3.0f}%"
    lines = ["```", f"{'verdict':9s}{'n':>5s}  {'15 min':>10s}  {'1 h':>10s}  {'3 h':>10s}  {'best 3h':>10s}",
             "median net after tax · share in profit"]
    for key in ("BUY", "WATCH", "SKIP", "LEARNING", "ALL dips"):
        g = groups.get(key)
        if not g:
            continue
        lines.append(f"{key:9s}{len(g):5d}  " + "  ".join(f"{fmt([r[i] for r in g]):>10s}" for i in range(4)))
    lines.append("```")
    total = db.execute("select count(*) from dip_events").fetchone()[0]
    done = db.execute("select count(*) from dip_events where xm60 is not null and xm60<900").fetchone()[0]
    lines.append(f"Events logged **{total}** · with a 1 h result **{done}** · a BUY needs "
                 f"≥{SCORER_MIN_N} similar past events, a lower bound above 0 and ≥{SCORER_MIN_WIN * 100:.0f}% wins")
    return [{"title": f"🧪 Dip Scorer · shadow scoreboard · {PLATFORM_LABEL}",
             "description": "\n".join(lines), "color": 0x1ABC9C,
             "footer": {"text": "Shadow mode: verdicts are logged, never posted as buy alerts"}}]


SCORER_POST_HOURS = float(os.environ.get("FC_SCORER_POST_HOURS", "0"))   # 0 = never post the scoreboard


def maybe_post_scorer(db, now: float, dry_run: bool) -> None:
    if SCORER_POST_HOURS <= 0:
        return
    last = get_meta(db, "scorer_post_ts")
    if last and now - float(last) < SCORER_POST_HOURS * 3600 - 120:
        return
    embeds = scorer_report(db, now)
    if embeds:
        post(embeds, dry_run)
        set_meta(db, "scorer_post_ts", now)


# ------------------------------------------------------------------- scan --
def _is_premium(card: Card) -> bool:
    """True for Icons and Heroes (matched on words in the rarity name)."""
    kind = card.kind.lower()
    return any(word in kind for word in PREMIUM_WORDS)


# Premium list: Icons/Heroes (and their holos) above PREMIUM_ICON_HERO_MIN, plus any other
# special card (promo, TOTW ... and their holos) above PREMIUM_PROMO_MIN. These cards get the
# sale-based premium dip detector only and are left out of the other dip posts.
PREMIUM_ICON_HERO_MIN = parse_price(os.environ.get("FC_PREMIUM_ICON_HERO_MIN", 100_000))
PREMIUM_PROMO_MIN = parse_price(os.environ.get("FC_PREMIUM_PROMO_MIN", 300_000))


PREMIUM_LIQUID_MIN_SALES = int(os.environ.get("FC_PREMIUM_LIQUID_MIN_SALES", "20"))   # sales in 4 days
_LIQUID_CARDS: set = set()       # cards with at least that many inferred sales; refreshed every scan


# ---- thin cards: erratic floors with few real sales are left out of the drop alerts ----------
THIN_MIN_SALES = int(os.environ.get("FC_THIN_MIN_SALES", "15"))     # inferred sales in 4 days
THIN_MAX_SPREAD_PCT = float(os.environ.get("FC_THIN_MAX_SPREAD", "60"))   # 24 h floor p10-p90 over median
THIN_FLOOR_OVER_SOLD = 2.0         # floor above this multiple of its 4-day sold median is not a real price


def thin_cards(db, now: float) -> set:
    """Cards that should not raise drop alerts: fewer than THIN_MIN_SALES inferred sales in 4 days, or
    a floor so erratic (p10-p90 of the last 24 h wider than THIN_MAX_SPREAD_PCT of its median) that a
    'dip' is just a stray listing. Cached for 10 minutes."""
    c = _cache.get("thin")
    if c and now - c[0] < 600:
        return c[1]
    prices_sold: dict = {}
    for cid, price in db.execute("select card_id, price from sales_inferred where kind='sale' and ts>=?",
                                 (now - PREMIUM_SALE_WINDOW_H * 3600,)):
        prices_sold.setdefault(cid, []).append(price)
    sales = {cid: len(v) for cid, v in prices_sold.items()}
    thin: set = set()
    # floor far above anything the card sells for: the cheap listings are gone and what is left is not the market
    for cid, price in db.execute("select card_id, price from prices where ts=(select max(ts) from prices)"):
        v = prices_sold.get(cid)
        if v and len(v) >= 10 and price > THIN_FLOOR_OVER_SOLD * statistics.median(v):
            thin.add(cid)
    seen: set = set()
    cur, buf = None, []

    def close():
        if cur is None or len(buf) < 50:
            return
        seen.add(cur)
        med = statistics.median(buf)
        spread = (buf[9 * len(buf) // 10] - buf[len(buf) // 10]) / med * 100 if med else 0
        if sales.get(cur, 0) < THIN_MIN_SALES or spread > THIN_MAX_SPREAD_PCT:
            thin.add(cur)
    for cid, price in db.execute("select card_id, price from prices where ts>=? order by card_id, price",
                                 (now - 86400,)):
        if cid != cur:
            close()
            cur, buf = cid, []
        buf.append(price)
    close()
    _cache["thin"] = (now, thin)
    return thin


def refresh_liquid_cards(db, now: float) -> None:
    global _LIQUID_CARDS
    rows = db.execute("select card_id from sales_inferred where kind='sale' and ts>=? group by card_id "
                      "having count(*)>=?", (now - PREMIUM_SALE_WINDOW_H * 3600, PREMIUM_LIQUID_MIN_SALES))
    _LIQUID_CARDS = {r[0] for r in rows}


def _in_premium_list(card: Card, usual: float | None = None) -> bool:
    """Judged on the higher of the current price and its 24 h median, so a card that
    dips below the threshold stays in the premium list instead of dropping out of it."""
    floor = PREMIUM_ICON_HERO_MIN if _is_premium(card) else PREMIUM_PROMO_MIN
    value = max(card.price, usual or 0)
    if value >= floor:
        return True
    # a well-traded card of 100k+ has plenty of sold prices to judge by, whatever its promo
    return value >= PREMIUM_ICON_HERO_MIN and card.card_id in _LIQUID_CARDS


def _is_totw(card: Card) -> bool:
    kind = card.kind.lower()
    return "team of the week" in kind or kind.startswith("totw")


def _is_totw_fodder(card: Card) -> bool:
    """True for TOTW cards below the minimum price threshold.

    These cards tend to sit at EA's minimum price and generate spurious
    min-price fluctuation alerts that add noise without actionable signal.
    """
    return _is_totw(card) and card.price < TOTW_MIN_PRICE


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


def scan(db, cards: list[Card], now: float, watchlist: set[str] | None = None,
         spikes_on: bool = True, purchased: dict | None = None) -> list[dict]:
    dips, spikes, flash_spikes, flash_dips = [], [], [], []
    spikes_on = spikes_on and SPIKE_ALERTS
    premium_dips: list[tuple[Card, dict]] = []   # (card, stats) – own embed
    cards_map = {c.card_id: c for c in cards}
    muted_new = new_card_ids(db, now)
    watch = watchlist or set()
    try:
        refresh_liquid_cards(db, now)
    except Exception as e:
        print(f"[warn] liquid-card refresh failed: {e}")
    auto = auto_watch_ids(db, now)
    watch_hits: list = []
    watch_sells: list = []
    db.execute("delete from watch_positions where buy_ts < ?", (now - WATCH_POSITION_EXPIRE_HOURS * 3600,))
    open_watch_positions = {r[0] for r in db.execute("select card_id from watch_positions")}
    stats = {c.card_id: card_stats(db, c, now) for c in cards
             if c.card_id not in muted_new and c.card_id not in watch}
    adj6, adj24, adjf, _ = market_moves(db, cards, stats, muted_new | watch)
    try:
        record_dip_events(db, cards, stats, adj24, muted_new | watch, now)   # long-term dip ledger
    except Exception as e:                                                    # never break a scan
        print(f"[warn] dip ledger failed: {e}")
    try:
        prune_active_sales(db, cards, now)
    except Exception as e:
        print(f"[warn] active sales prune failed: {e}")

    try:
        thin = thin_cards(db, now)
    except Exception as e:
        thin = set()
        print(f"[warn] thin-card filter failed: {e}")
    try:
        beh_types = _behaviour_cached(db, now)
    except Exception:
        beh_types = {}
    try:
        refresh_volume_cards(db, cards, now)
    except Exception as e:
        print(f"[warn] volume list refresh failed: {e}")
    vol_list = load_volume_list()
    for c in cards:
        if c.card_id in thin:
            continue                              # erratic floor / almost no sales: no drop alerts
        if c.card_id in watch or c.card_id in open_watch_positions:
            # watched cards skip the price/new-card filters and the normal lists;
            # they fire buy signals and sell signals to complete the trading cycle.
            s = watch_sell(db, c, now)
            if s:
                watch_sells.append((c, s))
            elif c.card_id in watch:
                d = watch_dip(db, c, now)
                if d:
                    watch_hits.append((c, d))
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
        in_vol = c.card_id in vol_list
        if c.price >= FLASH_MIN_PRICE and (not in_premium or in_vol):
            f = card_flash_stats(db, c)
            if f and f["prev_price"] > 0:
                pct = (c.price - f["prev_price"]) / f["prev_price"] * 100
                mf = adjf.get(rgroup(c), 0.0)
                rel = pct - mf
                f = dict(f, mkt=mf, raw=pct)
                if pct >= FLASH_SPIKE_PCT:
                    flash_spikes.append((c, f, pct))
                elif rel <= -(FAST_VOLUME_DIP_PCT if in_vol else FLASH_DIP_PCT):
                    sold = sm.sale_stats(db, c.card_id, now, PREMIUM_SALE_WINDOW_H, PREMIUM_HALFLIFE_H)
                    liquid = bool(sold) and (in_vol or sold["n24"] >= FAST_LIQUID_N24) and sold["fair"] > 0
                    if sold and (liquid or sold["n"] >= FAST_MIN_SALES) and sold["fair"] > 0:
                        under = (sold["fair"] - c.price) / sold["fair"] * 100
                        if liquid or under >= FAST_MIN_UNDER_SOLD:      # drops that stay near/above sold lost money
                            flash_dips.append((c, dict(f, vol=in_vol, tier=drop_tier(c, beh_types.get(c.card_id), under, sold["fair"])), rel))

    dips = fresh_dips(db, dips, now, "dip") if DIP6H_ALERTS else dips   # off: still scored and logged below
    flash_dips = fresh_dips(db, flash_dips, now, "flashdip")
    premium_dips = fresh_dips(db, premium_dips, now, "prem")

    # chance of a sale within the horizon, and log each dip for later review (outcomes table)
    for items, dtype in ((dips, "6h"), (flash_dips, "micro")):
        for c, d, rel in items:
            d["sellp"] = sm.sell_prob(db, rgroup(c), price_band(c.price), SCORE_HORIZON, now)
            log_outcome(db, c, dtype, rel, d.get("mkt", 0.0), now)
    if not DIP6H_ALERTS:
        dips = []                                  # 6 h dips are logged to `outcomes` for research, not posted
    for c, d in premium_dips:
        if d.get("sellp") is None:
            d["sellp"] = sm.sell_prob(db, rgroup(c), price_band(c.price), SCORE_HORIZON, now)
        log_outcome(db, c, "prem", d["dip_pct"], d.get("mkt", 0.0), now)
    db.commit()

    if spikes_on:
        spikes = fresh_spikes(db, spikes, now, "spike")
        flash_spikes = fresh_spikes(db, flash_spikes, now, "flash")
    auto_watch_update(db, cards, now, muted_new | (watch - auto))   # auto picks are re-screened each time
    embeds = (
        build_dips(dips)
        + (build_spikes(spikes) if spikes_on else [])
        + build_flashes(flash_spikes if spikes_on else [], flash_dips)
        + build_premium_dips(premium_dips)    # 💎 premium dips on the 24 h median fallback
    )

    # ── watchlist: buy signals and sell alerts for watched cards ─────────────
    embeds += build_watch_dips(watch_hits, auto)
    embeds += build_watch_sells(watch_sells, auto)

    # ── purchased list: spikes only for cards you hold ───────────────────────
    if purchased:
        embeds += build_owned_sells(owned_sells(db, cards_map, purchased, now))
        embeds += build_owned_spikes(owned_spikes(db, cards_map, purchased, now))

    # 🚨 premium below its 4-day sold price goes out LAST, so it sits at the bottom of the channel,
    # right where you look first
    embeds += build_premium_dips(premium_dips, hot=True)

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
            write_bid_sheet(db, cards, now)
        except Exception as e:
            print(f"[warn] bid sheet failed: {e}")
        try:
            record_index_hours(db, now)                        # permanent hourly index record
            maybe_post_index(db, now, dry_run)                 # market index, every few hours
            maybe_post_summary(db, now, dry_run)               # 12-hour summary, at 7 and 19 Austin time
            maybe_post_buy_windows(db, now, dry_run)           # buy windows, once a day
            maybe_post_scorer(db, now, dry_run)                # shadow dip-scorer scoreboard
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
