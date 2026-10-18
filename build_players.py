"""
Build players.json (special cards, quality_id=10) for fc_market_scanner.py.

OFFLINE (default, no requests made by this script):
  1. In your browser open, one page at a time (page=1, 2, 3 ... until the
     response ends with "next":null):
       https://www.fut.gg/api/fut/players/v2/27/?page=1&quality_id=10&response_format=card-image
  2. Save each page (Ctrl+S) into a folder called  pages  next to this script
     (same folder as build_players.py, wherever you run it from).
  3. python build_players.py          -> writes players.json

DOWNLOAD (opt-in):  python build_players.py --download [--max-pages 100] [--delay 5]
  Fetches those pages for you, slowly, and keeps them in pages/ so a stopped run
  can resume. fut.gg's robots.txt asks bots not to use this API, so only use this
  after checking their terms of use. Run it rarely (e.g. when new cards release).
"""
import argparse
import glob
import json
import os
import time
import urllib.request

URL = ("https://www.fut.gg/api/fut/players/v2/27/"
       "?page={page}&quality_id=10&response_format=card-image")
HERE = os.path.dirname(os.path.abspath(__file__))   # always next to this script
PAGES_DIR = os.path.join(HERE, "pages")
OUT = os.path.join(HERE, "players.json")
DEFS_DIR = os.path.join(HERE, "defs")                # holo lookup pages (see holo_lookup_urls.txt)
HOLO_OUT = os.path.join(HERE, "holo_ids.json")
MAX_TOTAL = 2000          # the special-card list is ~500; far more means the filter was ignored
# Standard (non-special) rarities. The list should never contain them; if it does
# (an unfiltered download), merge() drops them so players.json stays special-only.
STANDARD_RARITIES = {"common", "rare"}
# Rarities that are not tradable: dropped from players.json. Add more (lower-case).
EXCLUDE_RARITIES = {"base hall of fut"}
# Flags in fut.gg's definition-data records (defs/) that mark a card as SBC/objective/
# untradable reward. A card with any of these set is dropped, if its record is in defs/.
UNTRADABLE_FLAGS = ("isSbcItem", "isObjectiveItem", "isUntradeable",
                    "sbcSetEaId", "objectiveGroupEaId")


def load_page(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch(page: int) -> dict:
    req = urllib.request.Request(URL.format(page=page), headers={
        "Referer": "https://www.fut.gg/players/",
        "User-Agent": "Mozilla/5.0 (personal use, low rate)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(max_pages: int, delay: float, max_total: int = MAX_TOTAL) -> None:
    os.makedirs(PAGES_DIR, exist_ok=True)
    page, fetched_any = 1, False
    while page <= max_pages:
        path = os.path.join(PAGES_DIR, f"page_{page:03d}.json")
        if os.path.exists(path):                 # resume: reuse saved page
            data = load_page(path)
        else:
            if fetched_any:
                time.sleep(delay)                # be gentle between requests
            data = fetch(page)
            fetched_any = True
            if data.get("currentPage") != page:
                raise SystemExit(f"Asked for page {page}, got "
                                 f"{data.get('currentPage')} - stopping.")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            total = data.get("total") or 0
            if total > max_total:
                os.remove(path)
                raise SystemExit(
                    f"The server reports {total} cards, but the special-card list should be about "
                    f"500: the quality_id=10 filter was not applied to this request, so nothing "
                    f"more will be downloaded. Save the pages from your browser instead (open the "
                    f"URL, Ctrl+S into pages/), which does apply the filter. "
                    f"(Override with --max-total if you really want more than {max_total}.)")
            print(f"downloaded page {page} (total {total})")
        nxt = data.get("next")
        if not nxt or nxt <= page:
            break
        page = nxt
    else:
        print(f"[info] stopped at --max-pages {max_pages}; more pages exist")


def drop_untradable(rows: dict) -> None:
    """Remove cards that cannot be traded: excluded rarities, plus SBC / objective /
    untradable rewards known from the definition records in defs/."""
    by_rarity = [eid for eid, r in rows.items()
                 if (r.get("rarityName") or "").strip().lower() in EXCLUDE_RARITIES]
    for eid in by_rarity:
        del rows[eid]
    flagged = set()
    for fp in sorted(glob.glob(os.path.join(DEFS_DIR, "*.json")) + glob.glob(os.path.join(DEFS_DIR, "*.txt"))):
        try:
            recs = load_page(fp)["data"]
        except (ValueError, KeyError, TypeError):
            continue
        for rec in recs:
            if rec.get("eaId") in rows and rec.get("holographicType") != "holographic" \
                    and any(rec.get(f) for f in UNTRADABLE_FLAGS):
                flagged.add(rec["eaId"])
    for eid in flagged:
        del rows[eid]
    if by_rarity or flagged:
        print(f"[info] dropped untradable cards: {len(by_rarity)} by rarity, "
              f"{len(flagged)} SBC/objective/untradable by flag")


def add_holos(rows: dict) -> None:
    """Holo cards are separate EA ids that the list endpoint hides. Pages saved
    in defs/ (from holo_lookup_urls.txt) describe them; each holo whose normal
    card is in the list becomes its own row, flagged in holo_ids.json."""
    files = sorted(glob.glob(os.path.join(DEFS_DIR, "*.json"))
                   + glob.glob(os.path.join(DEFS_DIR, "*.txt")))
    if not files:
        return
    holo_ids, orphans = set(), 0
    for fp in files:
        try:
            recs = load_page(fp)["data"]
        except (ValueError, KeyError, TypeError):
            print(f"[skip] {fp}: not a definition-data page")
            continue
        for rec in recs:
            if rec.get("holographicType") != "holographic":
                continue
            base = rows.get(rec.get("standardItemEaId") or rec.get("basePlayerEaId"))
            if base is None:
                orphans += 1                   # normal card is not a special card we track
                continue
            row = dict(base)
            row["eaId"] = rec["eaId"]
            row["url"] = rec.get("url", base.get("url"))
            row["holographicType"] = "holographic"
            rows[rec["eaId"]] = row
            holo_ids.add(rec["eaId"])
    with open(HOLO_OUT, "w", encoding="utf-8") as f:
        json.dump(sorted(holo_ids), f)
    print(f"holos: {len(holo_ids)} added, wrote {HOLO_OUT}"
          + (f" ({orphans} holo(s) skipped: normal card not in the list)" if orphans else ""))


def merge() -> None:
    files = sorted(glob.glob(os.path.join(PAGES_DIR, "*.json"))
                   + glob.glob(os.path.join(PAGES_DIR, "*.txt")))
    if not files:
        raise SystemExit(f"No saved pages found in {PAGES_DIR}\n"
                         "Create that folder and save the fut.gg pages into it "
                         "(.json or .txt files).")
    rows: dict[int, dict] = {}
    dropped = 0
    seen: set[int] = set()
    last: dict | None = None
    for fp in files:
        try:
            data = load_page(fp)
            page_rows = data["data"]
        except (ValueError, KeyError, TypeError):
            print(f"[skip] {fp}: not a player page")
            continue
        for row in page_rows:
            if (row.get("rarityName") or "").strip().lower() in STANDARD_RARITIES:
                dropped += 1                     # not a special card
                continue
            rows[row["eaId"]] = row
        cp = data.get("currentPage", 0)
        seen.add(cp)
        if last is None or cp >= last.get("currentPage", 0):
            last = data
    if dropped:
        print(f"[info] ignored {dropped} standard (Common/Rare) cards: the pages were not special-only")
    drop_untradable(rows)
    add_holos(rows)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"data": list(rows.values())}, f, ensure_ascii=False)
    print(f"wrote {OUT}: {len(rows)} cards (holos included) from {len(seen)} page(s)")
    if seen:
        missing = sorted(set(range(1, max(seen) + 1)) - seen)
        if missing:
            print(f"[warn] missing pages: {missing}")
    if last and last.get("next"):
        print(f"[warn] last saved page has next={last['next']} - list is incomplete")
    elif last:
        print(f"complete list (site reports total={last.get('total')})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--max-pages", type=int, default=100)
    ap.add_argument("--delay", type=float, default=5.0)
    ap.add_argument("--max-total", type=int, default=MAX_TOTAL)
    args = ap.parse_args()
    if args.download:
        download(args.max_pages, args.delay, args.max_total)
    merge()
