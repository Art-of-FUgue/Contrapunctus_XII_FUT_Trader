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
import os
import statistics

LISTING_HOURS = tuple(float(x) for x in os.environ.get("FC_LISTING_HOURS", "1,3,6,12").split(",") if x)
TOL = float(os.environ.get("FC_SALE_TOL_SECONDS", "90"))   # extra slack around each duration
MAX_GAP = 1200                     # seconds; a longer gap between scans resets a card's state
STACK_MAX = 12
MAX_WINDOW = 1800                  # seconds; wider age windows are not used for sell chance
KEEP_DAYS = 14


def init(db) -> None:
    db.execute("create table if not exists floor_state(card_id text primary key, state text)")
    db.execute("create table if not exists sales_inferred(card_id text, ts real, price int, "
               "g text, band text, kind text, age_lo real, age_hi real)")
    db.execute("create index if not exists sx on sales_inferred(card_id, ts)")


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
        return {"p": price, "ts": now,
                "stack": [{"p": price, "t0": None, "lo": None, "seen": now}]}, []
    stack, events, prev, s = state["stack"], [], state["p"], state["ts"]
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


def update(db, rows, now: float) -> int:
    """rows: iterable of (card_id, price, group, band). Records inferred sales."""
    n = 0
    for cid, price, g, band in rows:
        r = db.execute("select state from floor_state where card_id=?", (cid,)).fetchone()
        state, events = step(json.loads(r[0]) if r else None, int(price), now)
        db.execute("insert or replace into floor_state values (?,?)", (cid, json.dumps(state)))
        for kind, p, lo, hi in events:
            if kind in ("sale", "ambiguous"):
                db.execute("insert into sales_inferred values (?,?,?,?,?,?,?,?)",
                           (cid, now, p, g, band, kind, lo, hi))
                n += 1
    db.execute("delete from sales_inferred where ts < ?", (now - KEEP_DAYS * 86400,))
    db.commit()
    return n


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
            (*args, now - KEEP_DAYS * 86400)).fetchall()
        if len(rows) >= min_n:
            pts = [(((lo + hi) / 2) / 3600, 1 if kind == "sale" else 0)
                   for kind, lo, hi in rows if hi - lo <= MAX_WINDOW]   # wide windows are too vague
            return _km(pts, horizon_h)
    return None
