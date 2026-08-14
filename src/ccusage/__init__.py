#!/usr/bin/env python3
"""ccusage - Claude Code usage monitor.

Fetches rate limit data from Anthropic's /api/oauth/usage endpoint
using your Claude Code OAuth token. Zero external dependencies.

Usage:
    ccusage              Show current usage (colored)
    ccusage status       Same as above
    ccusage json         Print raw JSON
    ccusage daemon       Run in foreground, refresh every 5 min, write to ~/.claude/usage-limits.json
    ccusage statusline   Claude Code statusline command (reads stdin + cache)
    ccusage install      Print setup instructions
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_TTY = sys.stdout.isatty()


def _claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override is not None else Path.home() / ".claude"


def _resolve_claude_path(relative: str) -> Path:
    """Return the first existing path for a file inside Claude's config directory.

    On Windows, if the native path doesn't exist, also checks WSL distros
    so the tool works from a Windows terminal against a WSL-based Claude Code
    installation.

    Args:
        relative: path relative to Claude's config directory, e.g. ".credentials.json"
    """
    native = _claude_config_dir() / relative
    if "CLAUDE_CONFIG_DIR" in os.environ or native.exists() or sys.platform != "win32":
        return native

    # Windows: try WSL paths
    try:
        out = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True, timeout=5,
        )
        decoded = out.stdout.decode("utf-16-le", errors="ignore")
        distros = [d.strip() for d in decoded.splitlines() if d.strip()]
    except Exception:
        return native

    for distro in distros:
        wsl_base = Path(f"//wsl$/{distro}/home")
        try:
            users = [p.name for p in wsl_base.iterdir() if p.is_dir()]
        except OSError:
            continue
        for user in users:
            candidate = wsl_base / user / ".claude" / relative
            if candidate.exists():
                return candidate

    return native


CLAUDE_DIR = _claude_config_dir()
CREDENTIALS_FILE = _resolve_claude_path(".credentials.json")
USAGE_FILE = _resolve_claude_path("usage-limits.json")
DAEMON_INTERVAL = 300  # 5 minutes
UNAVAILABLE_RETRY_INTERVAL = 3600  # 1 hour
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client


class UsageUnavailableError(RuntimeError):
    """The account is authenticated but its usage endpoint is unavailable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _usage_unavailable_error(error: urllib.error.HTTPError) -> UsageUnavailableError | None:
    """Translate known account-level authorization failures into a typed error."""
    if error.code != 403:
        return None
    try:
        payload = json.loads(error.read())
        api_error = payload.get("error") or {}
        details = api_error.get("details") or {}
        code = details.get("error_code")
        message = api_error.get("message")
    except (AttributeError, json.JSONDecodeError, TypeError):
        return None
    if code != "oauth_not_allowed_for_organization":
        return None
    return UsageUnavailableError(
        code,
        message or "OAuth authentication is not allowed for this organization.",
    )


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


def _credential_identity(creds: dict | None) -> tuple[object, object]:
    oauth = (creds or {}).get("claudeAiOauth", {})
    return oauth.get("accessToken"), oauth.get("refreshToken")


def _persist_credentials(updated: dict, expected_identity: tuple[object, object]) -> dict:
    latest = get_credentials()
    if latest and _credential_identity(latest) != expected_identity:
        return latest
    if latest:
        latest_oauth = latest.get("claudeAiOauth", {})
        updated_oauth = updated.get("claudeAiOauth", {})
        updated = {
            **latest,
            **updated,
            "claudeAiOauth": {**latest_oauth, **updated_oauth},
        }

    tmp: Path | None = None
    try:
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{CREDENTIALS_FILE.name}.", dir=CREDENTIALS_FILE.parent
        )
        tmp = Path(tmp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(json.dumps(updated))
        os.replace(tmp, CREDENTIALS_FILE)
    except OSError as exc:
        if tmp:
            try:
                tmp.unlink()
            except OSError:
                pass
        print(
            f"Warning: refreshed token but could not write {CREDENTIALS_FILE}: {exc}",
            file=sys.stderr,
        )
    return updated


def refresh_credentials(creds: dict) -> dict:
    """Refresh the OAuth access token and persist updated credentials.

    Anthropic rotates refresh tokens, so the new refreshToken MUST be written
    back to .credentials.json or Claude Code's stored one goes stale and the
    user gets logged out.
    """
    expected_identity = _credential_identity(creds)
    latest = get_credentials()
    if latest and _credential_identity(latest) != expected_identity:
        return latest

    oauth = creds.get("claudeAiOauth", {})
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise RuntimeError("OAuth token expired and no refresh token — open Claude Code to log in")

    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        unavailable = _usage_unavailable_error(e)
        if unavailable:
            raise unavailable from e
        latest = get_credentials()
        if e.code in (400, 401) and latest:
            if _credential_identity(latest) != expected_identity:
                return latest
        raise RuntimeError(f"Token refresh failed ({e.code}) — open Claude Code to refresh it") from e

    oauth = dict(oauth)
    oauth["accessToken"] = result["access_token"]
    if result.get("refresh_token"):
        oauth["refreshToken"] = result["refresh_token"]
    oauth["expiresAt"] = int(time.time() * 1000 + int(result.get("expires_in", 3600)) * 1000)
    updated = dict(creds)
    updated["claudeAiOauth"] = oauth

    return _persist_credentials(updated, expected_identity)


def fetch_usage() -> dict:
    """Fetch usage from Anthropic's /api/oauth/usage endpoint.

    Reads the OAuth token from ~/.claude/.credentials.json, auto-refreshing it
    (and persisting the rotated credentials) when expired or rejected.
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
        raise RuntimeError(f"No credentials at {CREDENTIALS_FILE} — run `claude` first")

    oauth = creds.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("No OAuth access token in credentials")

    # Refresh proactively if expired (or about to).
    if time.time() * 1000 > oauth.get("expiresAt", 0) - 60_000:
        creds = refresh_credentials(creds)
        token = creds["claudeAiOauth"]["accessToken"]

    refreshed_after_rejection = False
    for attempt in range(3):
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "claude-code/2.1.71",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            unavailable = _usage_unavailable_error(e)
            if unavailable:
                raise unavailable from e
            if e.code != 401:
                raise

            latest = get_credentials()
            latest_token = (latest or {}).get("claudeAiOauth", {}).get("accessToken")
            if latest_token and latest_token != token:
                creds, token = latest, latest_token
                continue
            if not refreshed_after_rejection:
                creds = refresh_credentials(latest or creds)
                token = creds["claudeAiOauth"]["accessToken"]
                refreshed_after_rejection = True
                continue
            raise

    raise RuntimeError("Failed to fetch usage after token refresh")


# Top-level keys in the cached usage dict that are NOT rate-limit buckets.
_META_KEYS = {
    "plan", "source", "updated_at", "last_success_at", "status",
    "unavailable", "extra_usage",
}


def _bucket_display(short_key: str) -> tuple[str, str]:
    """Return (full label, statusline abbrev) for a short bucket key.

    Derived from the key so unknown/newly-added buckets get a reasonable
    label automatically, e.g. "7d_fable" -> ("Week (Fable)", "fab").
    """
    known = {
        "session": ("Session", "sess"),
        "5h": ("Session (5h)", "5h"),
        "7d": ("Week (all)", "7d"),
        "7d_opus": ("Week (Opus)", "opus"),
        "7d_sonnet": ("Week (Sonnet)", "son"),
    }
    if short_key in known:
        return known[short_key]
    if short_key.startswith("7d_"):
        model = short_key[3:]
        return f"Week ({model.replace('_', ' ').title()})", model[:3]
    return short_key.replace("_", " ").title(), short_key[:4]


def _quota_buckets(data: dict):
    """Yield (short_key, bucket) for each rate-limit bucket in a usage dict.

    Relies on insertion order (build_usage_json inserts them sorted), so
    callers get a stable, sensible display sequence.
    """
    for key, val in data.items():
        if key in _META_KEYS:
            continue
        if isinstance(val, dict) and "pct" in val:
            yield key, val


def _buckets_from_limits(limits) -> list:
    """Parse the API's structured `limits` array into (order, short_key, bucket).

    This is the current API shape. Each entry is self-describing:
        {"kind": "session"|"weekly_all"|"weekly_scoped", "percent": 58,
         "resets_at": "...", "scope": {"model": {"display_name": "Fable"}}}
    Model-scoped weekly limits (Opus, Sonnet, Fable, ...) are keyed by their
    model name, so a newly added one appears automatically.
    """
    out = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        pct = entry.get("percent")
        if pct is None:
            continue
        kind = entry.get("kind")
        if kind == "session":
            short_key, order = "session", 0
        elif kind == "weekly_all":
            short_key, order = "7d", 1
        elif kind == "weekly_scoped":
            model = (entry.get("scope") or {}).get("model") or {}
            name = (model.get("display_name") or model.get("id") or "scoped").strip()
            short_key = "7d_" + name.lower().replace(" ", "_")
            order = {"7d_opus": 2, "7d_sonnet": 3}.get(short_key, 4)
        else:
            short_key, order = kind or "unknown", 5
        out.append((order, short_key, {"pct": pct, "resets_at": entry.get("resets_at")}))
    return out


def build_usage_json(api_data: dict, plan: str) -> dict:
    """Transform API response into our cached format.

    Reads the API's structured `limits` array — every quota it reports (session,
    weekly-all, and per-model weekly windows like Opus/Sonnet/Fable) is included
    and mapped to a short key, so a newly added one appears automatically
    instead of being dropped.
    """
    result = {
        "plan": plan,
        "source": "api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    limits = api_data.get("limits")
    buckets = _buckets_from_limits(limits) if isinstance(limits, list) else []
    for _, short_key, bucket in sorted(buckets, key=lambda b: b[0]):
        result[short_key] = bucket
    extra = api_data.get("extra_usage")
    if extra:
        result["extra_usage"] = extra
    return result


def build_unavailable_usage(
    error: UsageUnavailableError, plan: str, previous: dict | None = None
) -> dict:
    """Build a cache tombstone that replaces stale quota values."""
    result = {
        "plan": plan,
        "source": "api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "unavailable",
        "unavailable": {
            "code": error.code,
            "message": error.message,
            "hint": "no active subscription or organization OAuth disabled",
        },
    }
    if previous:
        last_success = previous.get("last_success_at")
        if not last_success and previous.get("status") != "unavailable":
            last_success = previous.get("updated_at")
        if last_success:
            result["last_success_at"] = last_success
    return result


def _unavailable_hint(data: dict) -> str | None:
    unavailable = data.get("unavailable")
    if data.get("status") != "unavailable" or not isinstance(unavailable, dict):
        return None
    return unavailable.get("hint") or unavailable.get("message") or "usage unavailable"


def _cache_unavailable(error: UsageUnavailableError) -> dict:
    data = build_unavailable_usage(error, get_plan(), _read_cache())
    write_usage_file(data)
    return data


def write_usage_file(data: dict):
    """Write usage data to ~/.claude/usage-limits.json."""
    USAGE_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _read_cache() -> dict | None:
    """Read the cached usage file, or None if missing/unreadable."""
    try:
        return json.loads(USAGE_FILE.read_text())
    except Exception:
        return None


def _is_429(err: Exception) -> bool:
    """True if the exception is (or wraps) an HTTP 429 rate-limit error."""
    if isinstance(err, urllib.error.HTTPError) and err.code == 429:
        return True
    return "429" in str(err)


def _cache_age_str(data: dict) -> str:
    """Return e.g. ' 3m ago' from a usage dict's updated_at, or '' if unknown."""
    try:
        updated = datetime.fromisoformat(data["updated_at"])
        secs = int((datetime.now(timezone.utc) - updated).total_seconds())
        m = secs // 60
        return f" {m // 60}h{m % 60}m ago" if m >= 60 else f" {m}m ago"
    except Exception:
        return ""


# -- CLI commands --

def cmd_status(raw_json=False):
    """Fetch and display current usage.

    Fetches fresh from the API, but on failure (rate limit, offline, expired
    token) falls back to the last cached usage rather than crashing.
    """
    stale = False
    try:
        api_data = fetch_usage()
        data = build_usage_json(api_data, get_plan())
    except UsageUnavailableError as e:
        data = _cache_unavailable(e)
    except Exception as e:
        cached = _read_cache()
        if cached is None:
            reason = "rate limited (HTTP 429) — try again shortly" if _is_429(e) else str(e)
            print(f"Could not fetch usage: {reason}", file=sys.stderr)
            sys.exit(1)
        data, stale = cached, True

    if raw_json:
        print(json.dumps(data, indent=2))
        return

    R = "\033[0;31m" if _TTY else ""
    Y = "\033[0;33m" if _TTY else ""
    G = "\033[0;32m" if _TTY else ""
    D = "\033[0;90m" if _TTY else ""
    RST = "\033[0m" if _TTY else ""

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

    print(f"Plan: {data.get('plan', '?')}")
    unavailable = _unavailable_hint(data)
    if unavailable:
        print(f"  Usage unavailable: {unavailable}")
        return
    for key, bucket in _quota_buckets(data):
        label = _bucket_display(key)[0]
        pct = bucket["pct"]
        reset = fmt_reset(bucket.get("resets_at"))
        print(f"  {label:20s} {color_pct(pct)}{D}{reset}{RST}")

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used = extra.get("used_credits", 0) / 100
        limit = extra.get("monthly_limit", 0) / 100
        print(f"  {'Extra usage':20s} ${used:.2f} / ${limit:.2f}")

    if stale:
        age = _cache_age_str(data)
        print(f"{D}  (cached{age} — live fetch failed){RST}", file=sys.stderr)


def cmd_refresh():
    """Fetch fresh usage from the API and write the cache file once, then exit.

    A one-shot equivalent of a single daemon tick — use it to force
    ~/.claude/usage-limits.json up to date without running the daemon.
    """
    try:
        api_data = fetch_usage()
    except UsageUnavailableError as e:
        data = _cache_unavailable(e)
        print(f"Updated {USAGE_FILE}")
        print(f"  Usage unavailable: {_unavailable_hint(data)}")
        return
    except Exception as e:
        reason = "rate limited (HTTP 429) — try again shortly" if _is_429(e) else str(e)
        print(f"Could not refresh usage: {reason}", file=sys.stderr)
        sys.exit(1)
    data = build_usage_json(api_data, get_plan())
    write_usage_file(data)
    pcts = " ".join(f"{key}:{int(b['pct'])}%" for key, b in _quota_buckets(data))
    print(f"Updated {USAGE_FILE}")
    if pcts:
        print(f"  {pcts}")


def cmd_daemon(interval: int = DAEMON_INTERVAL):
    """Run in foreground, refresh every `interval` seconds."""
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"ccusage daemon started (refreshing every {interval}s)")
    print(f"Writing to {USAGE_FILE}")

    backoff = 0
    while True:
        try:
            api_data = fetch_usage()
            plan = get_plan()
            data = build_usage_json(api_data, plan)
            write_usage_file(data)
            backoff = 0
            pcts = [f"{key}:{int(b['pct'])}%" for key, b in _quota_buckets(data)]
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {' '.join(pcts)}")
        except UsageUnavailableError as e:
            data = _cache_unavailable(e)
            backoff = UNAVAILABLE_RETRY_INTERVAL
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Usage unavailable: {_unavailable_hint(data)}; retrying in {backoff}s",
                file=sys.stderr,
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                backoff = min((backoff or interval) * 2, 3600)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 429 — backing off {backoff}s", file=sys.stderr)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}", file=sys.stderr)

        time.sleep(backoff or interval)


def _get_cached_usage(max_age: int = DAEMON_INTERVAL) -> dict:
    """Read cached usage, refreshing from API if stale or missing."""
    try:
        usage = json.loads(USAGE_FILE.read_text())
        updated = datetime.fromisoformat(usage["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age < max_age:
            return usage
    except Exception:
        pass
    # Cache is stale or missing — try to refresh
    try:
        api_data = fetch_usage()
        usage = build_usage_json(api_data, get_plan())
        write_usage_file(usage)
        return usage
    except UsageUnavailableError as e:
        return _cache_unavailable(e)
    except Exception:
        # Return whatever we had, even if stale
        try:
            return json.loads(USAGE_FILE.read_text())
        except Exception:
            return {}


def cmd_statusline():
    """Claude Code statusline command. Reads Claude's JSON from stdin + cached usage."""
    R = "\033[0;31m" if _TTY else ""
    Y = "\033[0;33m" if _TTY else ""
    G = "\033[0;32m" if _TTY else ""
    C = "\033[0;36m" if _TTY else ""
    D = "\033[0;90m" if _TTY else ""
    RST = "\033[0m" if _TTY else ""

    def color_pct(pct: int) -> str:
        c = R if pct >= 70 else Y if pct >= 50 else G
        return f"{c}{pct}%{RST}"

    def fmt_reset(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            reset = datetime.fromisoformat(iso)
            secs = int((reset - datetime.now(timezone.utc)).total_seconds())
            if secs <= 0:
                return ""
            m = secs // 60
            if m >= 60:
                return f"{m // 60}h{m % 60}m"
            return f"{m}m"
        except Exception:
            return ""

    # Read Claude Code's JSON from stdin
    try:
        cc = json.loads(sys.stdin.read())
    except Exception:
        cc = {}

    model = cc.get("model", {}).get("display_name", "?")
    cost = cc.get("cost", {}).get("total_cost_usd", 0)
    pwd = cc.get("workspace", {}).get("current_dir", "?")
    home = str(Path.home())
    if pwd.startswith(home):
        pwd = "~" + pwd[len(home):]

    cost_fmt = f"${cost:.2f}" if cost > 0 else "$0"

    # Read cached usage, refresh if stale or missing
    usage = _get_cached_usage()

    plan = usage.get("plan", "?")
    parts = [f"{D}{pwd}{RST}", f"[{C}{model}{RST}]"]

    if _unavailable_hint(usage):
        parts.append("usage:unavailable")

    # Auto-include every quota bucket present (a newly added one just appears).
    session_bucket = {}
    for key, bucket in _quota_buckets(usage):
        abbrev = _bucket_display(key)[1]
        parts.append(f"{abbrev}:{color_pct(int(bucket.get('pct', 0)))}")
        if key in {"session", "5h"}:
            session_bucket = bucket

    parts.append(f"| {cost_fmt} | {D}{plan}{RST}")

    reset = fmt_reset(session_bucket.get("resets_at"))
    if reset:
        parts.append(f"| {D}reset:{reset}{RST}")

    print(" ".join(parts))


def cmd_install():
    """Print setup instructions."""
    print("""ccusage setup
=============

1. Run the daemon (in a terminal, tmux, or systemd):
   ccusage daemon

2. Configure Claude Code statusline in ~/.claude/settings.json:
   {
     "statusLine": {
       "type": "command",
       "command": "ccusage statusline"
     }
   }

3. The statusline reads ~/.claude/usage-limits.json (written by the daemon)
   and shows: session, weekly all-models, and weekly scoped limits.
""")


def main():
    parser = argparse.ArgumentParser(description="Claude Code usage monitor")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Show current usage (default)")
    sub.add_parser("json", help="Print raw JSON")
    sub.add_parser("refresh", help="Fetch fresh usage and update the cache file once")
    daemon_parser = sub.add_parser("daemon", help="Run refresh daemon")
    daemon_parser.add_argument("-i", "--interval", type=int, default=DAEMON_INTERVAL,
                               help=f"Refresh interval in seconds (default: {DAEMON_INTERVAL})")
    sub.add_parser("statusline", help="Claude Code statusline (reads stdin + cache)")
    sub.add_parser("install", help="Print setup instructions")
    args = parser.parse_args()

    cmd = args.command or "status"
    if cmd == "status":
        cmd_status()
    elif cmd == "json":
        cmd_status(raw_json=True)
    elif cmd == "refresh":
        cmd_refresh()
    elif cmd == "daemon":
        cmd_daemon(interval=args.interval)
    elif cmd == "statusline":
        cmd_statusline()
    elif cmd == "install":
        cmd_install()


if __name__ == "__main__":
    main()
