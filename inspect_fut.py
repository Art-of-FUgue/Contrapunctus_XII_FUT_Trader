"""
Diagnostic: figure out how fut.gg's price files map to players.

1. In your browser open
     https://www.fut.gg/api/fut/players/v2/27/?response_format=card-image
   and save the page (Ctrl+S) as  players.json  next to this script.
   (Saved by hand so this script doesn't call that API.)
2. python inspect_fut.py
3. Paste the full output back to Claude.
Only needs the standard library.
"""
import json
import os
import urllib.request

BASE = "https://r2.fut.gg/27/"
HDR = {"Referer": "https://www.fut.gg/", "User-Agent": "Mozilla/5.0"}


def get(url):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def describe(name, obj):
    print(f"\n== {name} ==")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                print(f"  {k}: list len={len(v)} first30={v[:30]} last10={v[-10:]}")
            else:
                print(f"  {k}: {str(v)[:100]}")
    else:
        print(f"  type={type(obj).__name__}")


man = get(BASE + "manifest.json")
print("published_at:", man.get("_published_at"))
files = {}
for name in ("player-prices-index", "player-prices-pc", "player-prices-pc-dyn"):
    files[name] = get(f"{BASE}{name}.v1.{man[name]}.json")
    describe(f"{name} (hash {man[name]})", files[name])

# ---- players list (saved by hand) ----
players = []
if os.path.exists("players.json"):
    raw = json.load(open("players.json", encoding="utf-8"))
    players = raw["data"] if isinstance(raw, dict) else raw
print(f"\nplayers.json: {len(players)} players")
ea_ids = {p["eaId"] for p in players}
db_ids = {p["id"] for p in players}

# ---- test how the index decodes ----
idx = files["player-prices-index"]
d, id0 = idx["d"], idx.get("id0", 0)


def cumsum(start):
    out, t = [], start
    for x in d:
        t += x
        out.append(t)
    return out


print(f"\nindex: id0={id0} len(d)={len(d)}")
for label, vals in (("cumsum starting at id0", cumsum(id0)),
                    ("cumsum starting at 0", cumsum(0))):
    if players:
        m_ea = sum(v in ea_ids for v in vals)
        m_id = sum(v in db_ids for v in vals)
        print(f"  {label}: first10={vals[:10]}  matches eaId={m_ea}  matches id={m_id}")
    else:
        print(f"  {label}: first10={vals[:10]}")

for name in ("player-prices-pc", "player-prices-pc-dyn"):
    arr = files[name].get("p") or files[name].get("d") or []
    print(f"\n{name}: len={len(arr)}  (index len={len(d)})")
    small = sum(1 for x in arr if 0 < x < 1000)
    print(f"  values <1000: {small}  zeros/negatives: {sum(1 for x in arr if x <= 0)}")
