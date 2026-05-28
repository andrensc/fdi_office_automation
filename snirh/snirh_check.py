#!/usr/bin/env python3
"""
snirh_check.py — Quick SNIRH connectivity check with optional watch/polling mode.

Usage:
    python3 snirh/snirh_check.py                        # single check
    python3 snirh/snirh_check.py --watch                # poll every 60s until available
    python3 snirh/snirh_check.py --watch --interval 30  # poll every 30s
    python3 snirh/snirh_check.py --watch --then-run     # poll then auto-run refresh
"""
import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BASE_URL = "https://snirh.apambiente.pt"

CHECK_URLS = [
    (f"{BASE_URL}/index.php?idMain=1&idItem=9.6", "Seca bulletin"),
    (f"{BASE_URL}/index.php?idMain=1&idItem=1.3", "Albufeiras bulletin"),
    (f"{BASE_URL}/index.php?idMain=2&idItem=1",   "Station catalog"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def check_once(verbose: bool = True) -> bool:
    """Check all SNIRH endpoints. Returns True if all are reachable (HTTP 200)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    all_ok = True
    for url, label in CHECK_URLS:
        try:
            r = session.get(url, timeout=15)
            ok = r.status_code == 200
            if verbose:
                icon = "✓" if ok else "✗"
                size = f"{len(r.content):,} bytes" if ok else ""
                print(f"  {icon} {label:30s} HTTP {r.status_code}  {size}")
            if not ok:
                all_ok = False
        except requests.RequestException as exc:
            if verbose:
                print(f"  ✗ {label:30s} ERROR: {exc}")
            all_ok = False
    return all_ok


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Check SNIRH website availability",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll repeatedly until all endpoints return HTTP 200",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between poll attempts when --watch is active",
    )
    parser.add_argument(
        "--then-run",
        action="store_true",
        help="After successful check with --watch, automatically run snirh_refresh_all.py",
    )
    args = parser.parse_args()

    if args.watch:
        attempt = 0
        while True:
            attempt += 1
            ts = time.strftime("%H:%M:%S")
            print(f"\n[Attempt {attempt} @ {ts}] Checking SNIRH endpoints...")
            if check_once(verbose=True):
                print(f"\n✅ SNIRH is available!")
                if args.then_run:
                    print("▶ Running snirh_refresh_all.py ...\n")
                    refresh_script = Path(__file__).parent / "snirh_refresh_all.py"
                    result = subprocess.run(
                        [sys.executable, str(refresh_script)],
                        cwd=str(Path(__file__).resolve().parent.parent),
                    )
                    return result.returncode
                else:
                    print("Run:  python3 snirh/snirh_refresh_all.py")
                return 0
            else:
                next_ts = time.strftime(
                    "%H:%M:%S", time.localtime(time.time() + args.interval)
                )
                print(f"  ⏳ Unavailable. Next check at {next_ts} (Ctrl+C to stop)")
                try:
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    print("\nStopped by user.")
                    return 1
    else:
        print(f"Checking SNIRH availability ({BASE_URL})...")
        ok = check_once(verbose=True)
        if ok:
            print("\n✅ All SNIRH endpoints reachable — ready to scrape")
            return 0
        else:
            print("\n✗ Some endpoints unreachable. Try: python3 snirh/snirh_check.py --watch")
            return 1


if __name__ == "__main__":
    sys.exit(main())
