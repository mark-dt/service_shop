#!/usr/bin/env python3
"""
Python load generator for the Flask shop service.

- Runs forever
- Waits 5 seconds between every action
- Supports multiple concurrent users (threads), each with its own X-Session-Id
- Simple, resilient: continues on errors and logs them to stderr
"""

import argparse
import os
import sys
import time
import uuid
import random
import signal
import threading
from typing import Optional, Tuple

try:
    import requests
except ImportError:
    print("This script requires the 'requests' package. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

WAIT_SECONDS = 5  # REQUIRED: wait 5 seconds between any actions

CATALOG_ITEM_IDS = ["1", "2", "3", "4", "5"]  # must match app.py

def get_session_id_from_headers(headers) -> Optional[str]:
    # 'requests' lowercases header names internally
    # Try both standard and lowercase keys
    return headers.get("X-Session-Id") or headers.get("x-session-id")

def health_check(base_url: str, timeout: float = 5.0) -> bool:
    try:
        r = requests.get(f"{base_url}/healthz", timeout=timeout)
        return r.ok
    except Exception:
        return False

def ensure_session(session: requests.Session, base_url: str) -> str:
    """
    Initialize a session by calling /catalog and extracting X-Session-Id.
    Fallback to locally generated UUID if header isn't present.
    """
    try:
        r = session.get(f"{base_url}/catalog", timeout=5)
        sid = get_session_id_from_headers(r.headers)
        if sid:
            return sid
    except Exception as e:
        print(f"[worker] /catalog error: {e}", file=sys.stderr)

    # Fallback
    fallback_sid = str(uuid.uuid4())
    print(f"[worker] Using fallback session id: {fallback_sid}", file=sys.stderr)
    return fallback_sid

def do_add(session: requests.Session, base_url: str, session_id: str) -> Tuple[bool, Optional[str]]:
    item_id = random.choice(CATALOG_ITEM_IDS)
    try:
        r = session.post(
            f"{base_url}/cart/add",
            json={"item_id": item_id, "qty": 1},
            headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
            timeout=5,
        )
        ok = r.status_code == 200
        return ok, item_id
    except Exception as e:
        print(f"[worker] /cart/add error: {e}", file=sys.stderr)
        return False, None

def do_view_cart(session: requests.Session, base_url: str, session_id: str) -> bool:
    try:
        r = session.get(
            f"{base_url}/cart",
            headers={"X-Session-Id": session_id},
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[worker] /cart error: {e}", file=sys.stderr)
        return False

def do_checkout(session: requests.Session, base_url: str, session_id: str) -> bool:
    try:
        r = session.post(
            f"{base_url}/checkout",
            headers={"X-Session-Id": session_id},
            timeout=5,
        )
        # 200 OK on success, 400 if cart empty
        return r.status_code == 200
    except Exception as e:
        print(f"[worker] /checkout error: {e}", file=sys.stderr)
        return False

def worker(worker_id: int, base_url: str, checkout_rate: int):
    """
    One worker simulating a single user session forever:
      - GET /catalog   (initialize session)
      - wait 5s
      - POST /cart/add
      - wait 5s
      - GET /cart
      - wait 5s
      - sometimes POST /checkout (based on checkout_rate)
      - wait 5s (only if checkout was attempted)
      - repeat forever
    """
    session = requests.Session()
    session.headers.update({"User-Agent": f"shop-loadgen/1.0 worker/{worker_id}"})

    session_id = ensure_session(session, base_url)
    print(f"[worker {worker_id}] session_id={session_id}")

    # Required 5s wait between actions
    time.sleep(WAIT_SECONDS)

    while True:
        ok, item = do_add(session, base_url, session_id)
        if not ok:
            print(f"[worker {worker_id}] add failed (item={item})", file=sys.stderr)
        time.sleep(WAIT_SECONDS)

        ok = do_view_cart(session, base_url, session_id)
        if not ok:
            print(f"[worker {worker_id}] view cart failed", file=sys.stderr)
        time.sleep(WAIT_SECONDS)

        # random checkout with probability = checkout_rate%
        roll = random.randint(1, 100)
        if roll <= checkout_rate:
            ok = do_checkout(session, base_url, session_id)
            if not ok:
                # Cart may be empty -> 400, or request failed; that's fine for loadgen
                print(f"[worker {worker_id}] checkout did not succeed (likely empty cart or error)", file=sys.stderr)
            # Wait 5s after this action as well
            time.sleep(WAIT_SECONDS)

# --- Graceful shutdown handling ---
_shutdown = threading.Event()

def _handle_signal(signum, frame):
    print("\n[main] Signal received, stopping workers...", file=sys.stderr)
    _shutdown.set()

def main():
    parser = argparse.ArgumentParser(description="Python load generator for the shop service (runs forever).")
    parser.add_argument("-u", "--base-url", default=os.environ.get("BASE_URL", "http://localhost:8080"),
                        help="Base URL of the shop service (default: http://localhost:8080)")
    parser.add_argument("-c", "--users", type=int, default=int(os.environ.get("USERS", "1")),
                        help="Number of concurrent users (threads). Default: 1")
    parser.add_argument("-r", "--checkout-rate", type=int, default=int(os.environ.get("CHECKOUT_RATE", "10")),
                        help="Percentage (0-100) chance to attempt checkout each loop. Default: 10")
    parser.add_argument("--skip-health-check", action="store_true",
                        help="Skip initial /healthz check")
    args = parser.parse_args()

    # Enforce bounds
    args.users = max(1, args.users)
    args.checkout_rate = min(100, max(0, args.checkout_rate))

    if not args.skip_health_check:
        print(f"[main] Checking health at {args.base_url}/healthz ...")
        if not health_check(args.base_url):
            print(f"[main] Service not healthy or unreachable at {args.base_url}. Start it first (python3 app.py).", file=sys.stderr)
            sys.exit(2)

    # Register signal handlers for graceful exit
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    print(f"[main] Starting load: users={args.users}, checkout_rate={args.checkout_rate}%, wait={WAIT_SECONDS}s between actions")
    threads = []
    for wid in range(1, args.users + 1):
        t = threading.Thread(target=worker, args=(wid, args.base_url, args.checkout_rate), daemon=True)
        t.start()
        threads.append(t)

    # Run forever until interrupted
    try:
        while not _shutdown.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown.set()

    print("[main] Exiting...")

if __name__ == "__main__":
    main()
