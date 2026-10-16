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


def load_page(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch(page: int) -> dict:
    req = urllib.request.Request(URL.format(page=page), headers={
        "Referer": "https://www.fut.gg/players/",
        "User-Agent": "Mozilla/5.0 (personal use, low rate)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(max_pages: int, delay: float) -> None:
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
            print(f"downloaded page {page} (total {data.get('total')})")
        nxt = data.get("next")
        if not nxt or nxt <= page:
            break
        page = nxt
    else:
        print(f"[info] stopped at --max-pages {max_pages}; more pages exist")


def merge() -> None:
    files = sorted(glob.glob(os.path.join(PAGES_DIR, "*.json"))
                   + glob.glob(os.path.join(PAGES_DIR, "*.txt")))
    if not files:
        raise SystemExit(f"No saved pages found in {PAGES_DIR}\n"
                         "Create that folder and save the fut.gg pages into it "
                         "(.json or .txt files).")
    rows: dict[int, dict] = {}
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
            rows[row["eaId"]] = row
        cp = data.get("currentPage", 0)
        seen.add(cp)
        if last is None or cp >= last.get("currentPage", 0):
            last = data
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"data": list(rows.values())}, f, ensure_ascii=False)
    print(f"wrote {OUT}: {len(rows)} cards from {len(seen)} page(s)")
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
    args = ap.parse_args()
    if args.download:
        download(args.max_pages, args.delay)
    merge()
