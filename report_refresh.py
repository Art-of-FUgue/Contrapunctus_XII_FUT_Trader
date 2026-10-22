"""
Read the output of `python setup.py` after a card refresh, decide whether the rebuild is safe to
commit, and post the result to Discord.

    python report_refresh.py setup_output.txt
Exit code 0 = safe to commit, 1 = do NOT commit. Without DISCORD_WEBHOOK_URL it prints instead.
"""
import json
import os
import sys

import requests

BAD = ("!! WARNING", "list is incomplete", "missing pages", "No saved pages", "bundle has no")


def section(text: str, start: str, end: str) -> str:
    a = text.find(start)
    if a < 0:
        return ""
    a += len(start)
    b = text.find(end, a)
    return text[a:b if b > 0 else None].strip()


def post(embed: dict) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        print(json.dumps(embed, ensure_ascii=False, indent=2))
        return
    r = requests.post(hook, json={"embeds": [embed]}, timeout=15)
    if r.status_code >= 400:
        print(f"[error] Discord {r.status_code}: {r.text[:200]}")


def main(path: str) -> int:
    text = open(path, encoding="utf-8", errors="replace").read()
    problems = [ln for ln in text.splitlines() if any(b in ln for b in BAD)]
    new = section(text, "---- new cards ----", "---- status ----")
    status = section(text, "---- status ----", "STEP 2 NEEDED") or section(text, "---- status ----", "Done.")
    if problems:
        post({"title": "⚠️ Card refresh NOT applied", "color": 0xE74C3C,
              "description": ("The rebuilt card list looked incomplete, so nothing was committed:\n"
                              + "\n".join("• " + p.strip() for p in problems[:6]))[:3900]})
        return 1
    if "no new cards since the last run" in new:
        post({"title": "🔄 Card refresh: no new cards", "color": 0x95A5A6, "description": status[:1000]})
        return 0
    lines, used = [], 0
    for i, ln in enumerate(new.splitlines()):
        if used + len(ln) + 1 > 3600:                      # keep the embed under Discord's limit
            lines.append(f"... and {len(new.splitlines()) - i} more lines (see setup output in Actions)")
            break
        lines.append(ln)
        used += len(ln) + 1
    foot = " · ".join(x.strip() for x in status.splitlines() if x.strip() and not x.startswith("Done"))
    post({"title": "🆕 Card list updated", "color": 0x2ECC71,
          "description": "```\n" + "\n".join(lines) + "\n```", "footer": {"text": foot[:200]}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "setup_output.txt"))
