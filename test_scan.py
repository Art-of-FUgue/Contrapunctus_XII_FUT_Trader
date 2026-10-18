"""
Quick test runner for fc_market_scanner.py.
Runs immediately so you don't have to wait 15 minutes.

Usage:
    python test_scan.py              # tests real market data fetch (dry-run, no webhook needed)
    python test_scan.py --live       # tests real market fetch + sends status to DISCORD_WEBHOOK_URL
    python test_scan.py --demo       # tests Discord embed alert formatting with simulated price dips
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(HERE, "fc_market_scanner.py")


def main():
    parser = argparse.ArgumentParser(description="Quick scanner test utility")
    parser.add_argument("--live", action="store_true", help="Send results to DISCORD_WEBHOOK_URL (default: dry run)")
    parser.add_argument("--demo", action="store_true", help="Simulate price alert embeds to test Discord formatting")
    parser.add_argument("--webhook", type=str, default=None, help="Discord Webhook URL to use for this test run")
    args = parser.parse_args()

    env = os.environ.copy()
    if args.webhook:
        env["DISCORD_WEBHOOK_URL"] = args.webhook
        args.live = True

    cmd = [sys.executable, SCANNER]
    if args.demo:
        cmd.append("--demo")
        if not args.live:
            cmd.append("--dry-run")
        print("\n[>>] Running DEMO test (simulated price fluctuations)...")
    else:
        cmd.append("--once")
        if not args.live:
            cmd.append("--dry-run")
        print("\n[>>] Running LIVE market test scan (fetching fut.gg static index)...")

    t0 = time.time()
    res = subprocess.run(cmd, cwd=HERE, env=env)
    duration = time.time() - t0

    if res.returncode == 0:
        print(f"\n[OK] Test scan completed successfully in {duration:.2f}s!")
    else:
        print(f"\n[ERROR] Test scan exited with code {res.returncode}.")
        sys.exit(res.returncode)


if __name__ == "__main__":
    main()
