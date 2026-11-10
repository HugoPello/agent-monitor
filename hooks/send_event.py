#!/usr/bin/env python3
"""Claude Code hook: forward the event JSON on stdin to agent-monitor.

Design rules, because this runs inside every Claude Code session you enable
it for:

- Standard library only, Python 3.9+: Claude Code runs it with your system
  Python, not the project's virtualenv.
- Never breaks or slows down the agent: always exits 0, prints nothing, and
  gives up after a short timeout. Configure it as an ``async`` hook so it
  also runs in the background. Claude Code does not enforce ``timeout`` on
  async hooks, so the timeout below is the only thing that bounds it.
- The body is forwarded as-is. The server validates it.
- Proxies are ignored: payloads can contain code and must not leave the
  machine through an ``HTTP_PROXY`` the user happens to have set.

Environment variables (all optional):

- ``AGENT_MONITOR_URL``           default ``http://127.0.0.1:8765/events``
- ``AGENT_MONITOR_HOOK_TIMEOUT``  seconds, default ``2``
- ``AGENT_MONITOR_HOOK_LOG``      file to append errors to (for debugging)
"""

import os
import sys
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765/events"
DEFAULT_TIMEOUT_SECONDS = 2.0


def get_timeout():
    """Timeout from the environment, falling back to the default if invalid."""
    try:
        value = float(os.environ.get("AGENT_MONITOR_HOOK_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def send(body, url, timeout):
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # An empty ProxyHandler disables proxies taken from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        response.read()


def log_error(message):
    path = os.environ.get("AGENT_MONITOR_HOOK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass  # logging must never be the thing that fails


def main():
    try:
        body = sys.stdin.buffer.read()
        if body.strip():
            send(body, os.environ.get("AGENT_MONITOR_URL", DEFAULT_URL), get_timeout())
    except Exception as exc:  # by design: nothing may escape
        log_error(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
