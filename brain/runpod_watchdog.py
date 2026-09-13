"""
RunPod pod watchdog — spawned as a detached subprocess by RunPodManager.

Stops the pod when EITHER condition is met:
  1. The brain process (parent PID) has been dead for > GRACE_PERIOD_S
  2. The brain hasn't been seen alive for > max_duration_s (resets each check
     while brain is running — only fires as a backstop when everything else fails)

Uses only stdlib so it runs with no dependencies in the detached process.

Usage:
    python3 runpod_watchdog.py <pod_id> <parent_pid> <api_key> <max_duration_s>
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.request

# Filled by main() from argv — parsing at import made `import brain.runpod_watchdog`
# raise IndexError, which broke any test or tool that merely imported the module.
POD_ID = ""
PARENT_PID = 0
API_KEY = ""
MAX_DURATION_S = 8 * 3600.0

CHECK_INTERVAL_S = 60.0  # poll every minute
GRACE_PERIOD_S = 120.0  # wait 2 min after PID death before stopping

_stop_cleanly = False  # set True by SIGTERM — exit without stopping pod


def _handle_sigterm(signum, frame):
    global _stop_cleanly
    _stop_cleanly = True


signal.signal(signal.SIGTERM, _handle_sigterm)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_pod() -> None:
    query = json.dumps(
        {"query": f'mutation {{ podStop(input: {{podId: "{POD_ID}"}}) {{ id desiredStatus }} }}'}
    ).encode()
    try:
        req = urllib.request.Request(
            "https://api.runpod.io/graphql",
            data=query,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30)  # nosec B310 - constant RunPod API URL
    except Exception as e:
        # Best-effort — log to stderr (redirected to /dev/null in prod but visible in dev)
        print(f"[watchdog] podStop failed: {e}", file=sys.stderr)


def parse_args(argv: list[str]) -> tuple[str, int, str, float]:
    """(pod_id, parent_pid, api_key, max_duration_s) from the argv RunPodManager
    passes: <pod_id> <parent_pid> <api_key> [max_duration_s]."""
    if len(argv) < 3:
        raise SystemExit(
            "usage: runpod_watchdog.py <pod_id> <parent_pid> <api_key> [max_duration_s]"
        )
    max_s = float(argv[3]) if len(argv) > 3 else 8 * 3600.0
    return argv[0], int(argv[1]), argv[2], max_s


def main(argv: list[str] | None = None) -> None:
    global POD_ID, PARENT_PID, API_KEY, MAX_DURATION_S
    POD_ID, PARENT_PID, API_KEY, MAX_DURATION_S = parse_args(
        list(sys.argv[1:]) if argv is None else list(argv)
    )
    last_seen_alive = time.time()
    dead_since: float | None = None

    while True:
        time.sleep(CHECK_INTERVAL_S)

        if _stop_cleanly:
            # Normal shutdown — RunPodManager.stop() handles the API call
            sys.exit(0)

        if _is_alive(PARENT_PID):
            last_seen_alive = time.time()
            dead_since = None
        else:
            if dead_since is None:
                dead_since = time.time()

            if time.time() - dead_since >= GRACE_PERIOD_S:
                _stop_pod()
                sys.exit(0)

        # Backstop: brain hasn't been seen for max_duration_s
        if time.time() - last_seen_alive >= MAX_DURATION_S:
            _stop_pod()
            sys.exit(0)


if __name__ == "__main__":
    main()
