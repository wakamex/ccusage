#!/usr/bin/env python3
"""ccusage - Claude Code usage monitor.

Fetches rate limit data from Anthropic's /api/oauth/usage endpoint
using your Claude Code OAuth token. Zero external dependencies.

Usage:
    ccusage              Show current usage (colored)
    ccusage status       Same as above
    ccusage json         Print raw JSON
    ccusage daemon       Run in foreground, refresh every 5 min, write to ~/.claude/usage-limits.json
    ccusage install      Print setup instructions
"""

import argparse
import json
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
CREDENTIALS_FILE = CLAUDE_DIR / ".credentials.json"
USAGE_FILE = CLAUDE_DIR / "usage-limits.json"
DAEMON_INTERVAL = 300  # 5 minutes


def get_credentials() -> dict | None:
    """Read OAuth credentials from Claude Code's credentials file."""
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_plan(creds: dict | None = None) -> str:
    """Return plan info from credentials (rateLimitTier or subscriptionType)."""
    if creds is None:
        creds = get_credentials()
    if not creds:
        return "unknown"
    oauth = creds.get("claudeAiOauth", {})
    tier = oauth.get("rateLimitTier") or oauth.get("subscriptionType") or "unknown"
    return tier.removeprefix("default_claude_")


def fetch_usage() -> dict:
    """Fetch usage from Anthropic's /api/oauth/usage endpoint.

    Requires a valid (non-expired) OAuth token from ~/.claude/.credentials.json.
    The key header is `anthropic-beta: oauth-2025-04-20` — without it, the
    endpoint returns an auth error.

    Returns the raw API response, e.g.:
        {
            "five_hour": {"utilization": 35.0, "resets_at": "..."},
            "seven_day": {"utilization": 14.0, "resets_at": "..."},
            "seven_day_sonnet": {"utilization": 39.0, "resets_at": "..."},
            "seven_day_opus": null,
            "extra_usage": {"is_enabled": true, "monthly_limit": 100000, ...}
        }
    """
    creds = get_credentials()
    if not creds:
        raise RuntimeError("No credentials at ~/.claude/.credentials.json — run `claude` first")

    oauth = creds.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("No OAuth access token in credentials")

    expires_at = oauth.get("expiresAt", 0)
    if time.time() * 1000 > expires_at:
        raise RuntimeError("OAuth token expired — open Claude Code to refresh it")

    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ccusage/1.0",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def build_usage_json(api_data: dict, plan: str) -> dict:
    """Transform API response into our cached format."""
    result = {
        "plan": plan,
        "source": "api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, api_key in [
        ("5h", "five_hour"),
        ("7d", "seven_day"),
        ("7d_sonnet", "seven_day_sonnet"),
        ("7d_opus", "seven_day_opus"),
    ]:
        bucket = api_data.get(api_key)
        if bucket:
            result[key] = {
                "pct": bucket["utilization"],
                "resets_at": bucket.get("resets_at"),
            }
    extra = api_data.get("extra_usage")
    if extra:
        result["extra_usage"] = extra
    return result


def write_usage_file(data: dict):
    """Write usage data to ~/.claude/usage-limits.json."""
    USAGE_FILE.write_text(json.dumps(data, indent=2) + "\n")


# -- CLI commands --

def cmd_status(raw_json=False):
    """Fetch and display current usage."""
    api_data = fetch_usage()
    plan = get_plan()
    data = build_usage_json(api_data, plan)

    if raw_json:
        print(json.dumps(data, indent=2))
        return

    R = "\033[0;31m"
    Y = "\033[0;33m"
    G = "\033[0;32m"
    D = "\033[0;90m"
    RST = "\033[0m"

    def color_pct(pct):
        p = int(pct)
        c = R if p >= 70 else Y if p >= 50 else G
        return f"{c}{p}%{RST}"

    def fmt_reset(iso):
        if not iso:
            return ""
        try:
            reset = datetime.fromisoformat(iso)
            now = datetime.now(timezone.utc)
            secs = int((reset - now).total_seconds())
            if secs <= 0:
                return ""
            m = secs // 60
            if m >= 60:
                return f" resets {m // 60}h{m % 60}m"
            return f" resets {m}m"
        except Exception:
            return ""

    print(f"Plan: {plan}")
    for label, key in [
        ("Session (5h)", "5h"),
        ("Week (all)", "7d"),
        ("Week (Sonnet)", "7d_sonnet"),
        ("Week (Opus)", "7d_opus"),
    ]:
        bucket = data.get(key)
        if bucket:
            pct = bucket["pct"]
            reset = fmt_reset(bucket.get("resets_at"))
            print(f"  {label:20s} {color_pct(pct)}{D}{reset}{RST}")

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used = extra.get("used_credits", 0) / 100
        limit = extra.get("monthly_limit", 0) / 100
        print(f"  {'Extra usage':20s} ${used:.2f} / ${limit:.2f}")


def cmd_daemon(interval: int = DAEMON_INTERVAL):
    """Run in foreground, refresh every `interval` seconds."""
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"ccusage daemon started (refreshing every {interval}s)")
    print(f"Writing to {USAGE_FILE}")

    while True:
        try:
            api_data = fetch_usage()
            plan = get_plan()
            data = build_usage_json(api_data, plan)
            write_usage_file(data)
            pcts = []
            for key in ("5h", "7d", "7d_sonnet"):
                b = data.get(key)
                if b:
                    pcts.append(f"{key}:{int(b['pct'])}%")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {' '.join(pcts)}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}", file=sys.stderr)

        time.sleep(interval)


def cmd_install():
    """Print setup instructions."""
    script_path = Path(__file__).resolve().parent.parent.parent / "statusline.py"
    print(f"""ccusage setup
=============

1. Run the daemon (in a terminal, tmux, or systemd):
   python3 {Path(__file__).resolve()} daemon

2. Configure Claude Code statusline in ~/.claude/settings.json:
   {{
     "statusLine": {{
       "type": "command",
       "command": "{script_path}"
     }}
   }}

3. The statusline reads ~/.claude/usage-limits.json (written by the daemon)
   and shows: 5h session, 7d all-models, 7d Sonnet-specific limits.
""")


def main():
    parser = argparse.ArgumentParser(description="Claude Code usage monitor")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Show current usage (default)")
    sub.add_parser("json", help="Print raw JSON")
    daemon_parser = sub.add_parser("daemon", help="Run refresh daemon")
    daemon_parser.add_argument("-i", "--interval", type=int, default=DAEMON_INTERVAL,
                               help=f"Refresh interval in seconds (default: {DAEMON_INTERVAL})")
    sub.add_parser("install", help="Print setup instructions")
    args = parser.parse_args()

    cmd = args.command or "status"
    if cmd == "status":
        cmd_status()
    elif cmd == "json":
        cmd_status(raw_json=True)
    elif cmd == "daemon":
        cmd_daemon(interval=args.interval)
    elif cmd == "install":
        cmd_install()


if __name__ == "__main__":
    main()
