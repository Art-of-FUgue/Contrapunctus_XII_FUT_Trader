"""
Infer completed sales from floor-price moves, using EA's fixed listing durations.

The only price we see is the cheapest listing ("floor"). Every listing lasts one
of a few fixed durations (default 1, 3, 6 or 12 hours), so when the floor listing
disappears we can tell what happened from its age:

  * it vanished at an age that matches NO duration  -> it was SOLD, at exactly
    the price it was listed for (a real completed sale);
  * its age matches a duration (within the scan uncertainty) -> it probably just
    EXPIRED (kept as 'ambiguous' and never counted as a sale).

A listing's age is only known if we watched it appear: a floor DROP means a new
listing was created since the previous scan. Listings that were already there
("inherited") have no known age and are not classified.

State per card is a small stack of known listings, cheapest on top.
"""
import json
import math
import os
import statistics

LISTING_HOURS = tuple(float(x) for x in os.environ.get("FC_LISTING_HOURS", "1,3,6,12").split(",") if x)
TOL = float(os.environ.get("FC_SALE_TOL_SECONDS", "90"))   # extra slack around each duration
MAX_GAP = 1200                     # seconds; a longer gap between scans resets a card's state
STACK_MAX = 12
MAX_WINDOW = 1800                  # seconds; wider age windows are not used for sell chance
SALES_RAW_DAYS = int(os.environ.get("FC_SALES_RAW_DAYS", "4"))   # individual inferred sales kept this long
SALES_DAILY_KEEP_DAYS = 56                  # per-card daily sales summary kept this long


def init(db) -> None:
    db.execute("create table if not exists floor_state(card_id text primary key, state text)")
    db.execute("create table if not exists sales_inferred(card_id text, ts real, price int, "
               "g text, band text, kind text, age_lo real, age_hi real)")
    db.execute("create index if not exists sx on sales_inferred(card_id, ts)")
    db.execute("create table if not exists sales_daily(card_id text, day integer, n_sale integer, "
               "n_amb integer, med int, lo int, hi int, primary key(card_id, day))")


def classify(age_lo: float, age_hi: float) -> str:
    """'sale' if no listing duration can explain the vanish, 'ambiguous' if one can,
    'invalid' if the listing is older than the longest duration (stack went wrong)."""
    longest = max(LISTING_HOURS) * 3600
    if age_lo - TOL > longest:
        return "invalid"
    for d in LISTING_HOURS:
        if age_lo - TOL < d * 3600 < age_hi + TOL:
            return "ambiguous"
    return "sale"


def step(state, price: int, now: float):
    """Advance one card's state with a new floor price. Returns (state, events).

    events are (kind, price, age_lo, age_hi) for the listing that vanished.
    """
    if not state or now - state["ts"] > MAX_GAP:
        stack = [{"p": price, "t0": None, "lo": None, "seen": now}] if price > 0 else []
        return {"p": price, "ts": now, "stack": stack}, []
    stack, events, prev, s = state["stack"], [], state["p"], state["ts"]
    if price <= 0:                                      # nothing is listed any more
        if prev > 0:                                    # every known listing is gone
            top = stack.pop() if stack else None
            if top and top["p"] == prev and top["t0"] is not None:
                age_lo = max(0.0, s - top["t0"])
                events.append((classify(age_lo, now - top["lo"]), prev, age_lo, now - top["lo"]))
            elif top and top["p"] == prev:
                events.append(("inherited", prev, None, None))
            while stack:
                h = stack.pop()
                if h["t0"] is not None and h.get("seen") is not None:
                    age_lo = max(0.0, h["seen"] - h["t0"])
                    events.append((classify(age_lo, now - h["lo"]), h["p"], age_lo, now - h["lo"]))
        return {"p": 0, "ts": now, "stack": []}, events
    if prev <= 0:                                       # empty market: this is a new listing
        stack = [{"p": price, "t0": now, "lo": s, "seen": now}]
        return {"p": price, "ts": now, "stack": stack}, events
    if price < prev:                                    # a new, cheaper listing appeared
        stack.append({"p": price, "t0": now, "lo": s, "seen": now})
    elif price > prev:                                  # the floor listing vanished
        top = stack.pop() if stack else None
        if top and top["p"] == prev:
            if top["t0"] is not None:                   # we watched it appear: age is known
                age_lo = max(0.0, s - top["t0"])
                age_hi = now - top["lo"]
                events.append((classify(age_lo, age_hi), prev, age_lo, age_hi))
            else:
                events.append(("inherited", prev, None, None))
        while stack and stack[-1]["p"] < price:         # hidden listings that also went
            h = stack.pop()
            if h["t0"] is not None and h.get("seen") is not None:
                # it was last seen as the floor at h["seen"] and is gone by now: its
                # age window is wide, but if no duration fits it, it was sold
                age_lo = max(0.0, h["seen"] - h["t0"])
                events.append((classify(age_lo, now - h["lo"]), h["p"], age_lo, now - h["lo"]))
        if not stack or stack[-1]["p"] != price:        # new floor we had not seen
            stack.append({"p": price, "t0": None, "lo": None, "seen": now})
    del stack[:-STACK_MAX]
    if stack:
        stack[-1]["seen"] = now                         # the floor listing is present right now
    return {"p": price, "ts": now, "stack": stack}, events


def update(db, rows, now: float, band_fn=None) -> int:
    """rows: iterable of (card_id, price, group, band); price 0 means nothing is listed
    (band may be None, then band_fn(price of the gone listing) is used)."""
    n = 0
    for cid, price, g, band in rows:
        r = db.execute("select state from floor_state where card_id=?", (cid,)).fetchone()
        state, events = step(json.loads(r[0]) if r else None, int(price), now)
        db.execute("insert or replace into floor_state values (?,?)", (cid, json.dumps(state)))
        for kind, p, lo, hi in events:
            if kind in ("sale", "ambiguous"):
                db.execute("insert into sales_inferred values (?,?,?,?,?,?,?,?)",
                           (cid, now, p, g, band or (band_fn(p) if band_fn else ""), kind, lo, hi))
                n += 1
    db.commit()
    return n


def compact_sales(db, now: float) -> int:
    """Squeeze inferred sales older than SALES_RAW_DAYS (whole UTC days only) into one summary
    row per card per day, then drop the individual rows. Returns the rows removed."""
    cut = int((now - SALES_RAW_DAYS * 86400) // 86400) * 86400
    rows = db.execute("select card_id, ts, price, kind from sales_inferred where ts < ?", (cut,)).fetchall()
    if not rows:
        return 0
    by: dict = {}
    for cid, ts, price, kind in rows:
        by.setdefault((cid, int(ts // 86400)), []).append((price, kind))
    out = []
    for (cid, day), v in by.items():
        sold = [p for p, k in v if k == "sale"]
        out.append((cid, day, len(sold), len(v) - len(sold),
                    int(statistics.median(sold)) if sold else None,
                    min(sold) if sold else None, max(sold) if sold else None))
    db.executemany("insert or replace into sales_daily values (?,?,?,?,?,?,?)", out)
    db.execute("delete from sales_inferred where ts < ?", (cut,))
    db.execute("delete from sales_daily where day < ?", (int((now - SALES_DAILY_KEEP_DAYS * 86400) // 86400),))
    db.commit()
    return len(rows)


def sale_prices(db, card_id: str, since: float) -> list[int]:
    return [r[0] for r in db.execute(
        "select price from sales_inferred where card_id=? and kind='sale' and ts>=?",
        (card_id, since))]


def baseline(db, card_id: str, now: float, hours: float = 24, min_n: int = 3):
    """Median price of listings that really sold in the window (None if too few)."""
    p = sale_prices(db, card_id, now - hours * 3600)
    return statistics.median(p) if len(p) >= min_n else None


def _km(points, horizon_h: float) -> float | None:
    """Kaplan-Meier P(sold within horizon). points: (hours, sold?)."""
    pts = sorted(points)
    at_risk, surv = len(pts), 1.0
    i = 0
    while i < len(pts):
        t = pts[i][0]
        if t > horizon_h:
            break
        d = c = 0
        while i < len(pts) and pts[i][0] == t:
            d += pts[i][1]
            c += 1 - pts[i][1]
            i += 1
        if at_risk:
            surv *= 1 - d / at_risk
        at_risk -= d + c
    return 1 - surv


def sell_prob(db, g: str, band: str, horizon_h: float, now: float, min_n: int = 30):
    """Chance a listing at the floor sells within horizon_h, from watched listings
    (sold = event; expiry-consistent = censored at its age). None if too little data."""
    for where, args in (("g=? and band=?", (g, band)), ("g=?", (g,)), ("1=1", ())):
        rows = db.execute(
            f"select kind, age_lo, age_hi from sales_inferred where {where} and ts>=?",
            (*args, now - SALES_RAW_DAYS * 86400)).fetchall()
        if len(rows) >= min_n:
            pts = [(((lo + hi) / 2) / 3600, 1 if kind == "sale" else 0)
                   for kind, lo, hi in rows if hi - lo <= MAX_WINDOW]   # wide windows are too vague
            return _km(pts, horizon_h)
    return None


def _weighted_median(values, weights) -> float:
    pairs = sorted(zip(values, weights))
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def sale_stats(db, card_id: str, now: float, window_h: float = 96, halflife_h: float = 24):
    """Fair value and noise from the real (inferred) sales in the last window_h hours."""
    rows = db.execute("select ts, price from sales_inferred where card_id=? and kind='sale' "
                      "and ts>=? order by ts", (card_id, now - window_h * 3600)).fetchall()
    if not rows:
        return None
    prices = [p for _, p in rows]
    weights = [0.5 ** ((now - t) / 3600 / halflife_h) for t, _ in rows]
    med = statistics.median(prices)
    mad = statistics.median(abs(p - med) for p in prices)
    n24 = sum(1 for t, _ in rows if t >= now - 24 * 3600)
    rate = n24 / 24                                    # sales per hour (a lower bound)
    return {"n": len(rows), "n24": n24, "fair": _weighted_median(prices, weights),
            "sigma_pct": 1.4826 * mad / med * 100 if med else 0.0,
            "last3": statistics.median(prices[-3:]),
            "p3": 1 - math.exp(-3 * rate)}
