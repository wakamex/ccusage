#!/usr/bin/env python3
"""Claude Code statusline — reads usage from ~/.claude/usage-limits.json.

OS-agnostic (no jq, bc, date -d, or other platform-specific dependencies).

Output example:
  ~/code [Opus 4.6] 5h:35% 7d:14% son:39% | $0.42 | default_claude_max_5x | reset:1h44m
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

R = "\033[0;31m"
Y = "\033[0;33m"
G = "\033[0;32m"
C = "\033[0;36m"
D = "\033[0;90m"
RST = "\033[0m"

USAGE_FILE = Path.home() / ".claude" / "usage-limits.json"


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


def main():
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

    # Read cached usage
    try:
        usage = json.loads(USAGE_FILE.read_text())
    except Exception:
        usage = {}

    plan = usage.get("plan", "?")
    five_h = usage.get("5h", {})
    seven_d = usage.get("7d", {})
    sonnet = usage.get("7d_sonnet", {})

    parts = [f"{D}{pwd}{RST}", f"[{C}{model}{RST}]"]

    if five_h:
        parts.append(f"5h:{color_pct(int(five_h.get('pct', 0)))}")
    if seven_d:
        parts.append(f"7d:{color_pct(int(seven_d.get('pct', 0)))}")
    if sonnet:
        parts.append(f"son:{color_pct(int(sonnet.get('pct', 0)))}")

    parts.append(f"| {cost_fmt} | {D}{plan}{RST}")

    reset = fmt_reset(five_h.get("resets_at"))
    if reset:
        parts.append(f"| {D}reset:{reset}{RST}")

    print(" ".join(parts))


if __name__ == "__main__":
    main()
