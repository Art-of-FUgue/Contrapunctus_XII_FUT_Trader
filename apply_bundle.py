"""
Unpack a card-refresh bundle (made by userscript/fut_card_refresh.user.js) into pages/ and defs/,
so that `python setup.py` can rebuild players.json and holo_ids.json from it.

    python apply_bundle.py bundle.json
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        bundle = json.load(f)
    pages, defs = bundle.get("pages") or [], bundle.get("defs") or []
    if not pages:
        raise SystemExit("bundle has no list pages")
    for d in ("pages", "defs"):                       # always start from exactly this bundle
        shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)
        os.makedirs(os.path.join(HERE, d))
    for p in pages:
        with open(os.path.join(HERE, "pages", f"page_{p['currentPage']:03d}.json"), "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False)
    with open(os.path.join(HERE, "defs", "defs_01.json"), "w", encoding="utf-8") as f:
        json.dump({"data": defs}, f, ensure_ascii=False)
    print(f"bundle {bundle.get('created')}: {len(pages)} pages, {len(defs)} lookup records "
          f"(asked for {bundle.get('asked')})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bundle.json")
