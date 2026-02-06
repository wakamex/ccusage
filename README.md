# ccusage

Claude Code usage monitor. Fetches your real rate limit data from Anthropic's API and displays it in the Claude Code statusline.

## Example output

`ccusage` command:

```
Plan: max_5x
  Session (5h)         39%  resets 1h26m
  Week (all)           15%  resets 143h26m
  Week (Sonnet)        39%  resets 65h26m
  Extra usage          $0.00 / $1000.00
```

Claude Code statusline (updated every 5 min by the daemon):

```
~/projects/myapp [Opus 4.6] 5h:39% 7d:15% son:39% | $1.37 | max_5x | reset:1h26m
```

## Setup

```bash
# Run the daemon (keeps usage-limits.json updated every 5 minutes)
uv run ccusage daemon

# Or just check usage once
uv run ccusage
```

Configure the statusline in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /code/ccusage/statusline.py"
  }
}
```

## Commands

| Command | Description |
|---------|-------------|
| `ccusage` | Show current usage (colored terminal output) |
| `ccusage json` | Print raw JSON |
| `ccusage daemon` | Run in foreground, refresh every 5 min |
| `ccusage install` | Print setup instructions |

## How Claude Code rate limiting works

Discovered by inspecting Claude Code's bundled `cli.js` (v2.1.32).

### Data sources

Claude Code gets rate limit data from two places:

1. **`/api/oauth/usage` endpoint** — Called by the `/status` slash command. Returns utilization percentages and reset times for each rate limit bucket. Requires the `anthropic-beta: oauth-2025-04-20` header.

2. **Response headers on every API call** — Every message response includes headers like:
   - `anthropic-ratelimit-unified-{claim}-utilization` (0-100 float)
   - `anthropic-ratelimit-unified-{claim}-reset` (unix timestamp)
   - `anthropic-ratelimit-unified-status` (`allowed` / `allowed_warning` / `rejected`)
   - `anthropic-ratelimit-unified-fallback` (`available` when fallback models are available)
   - `anthropic-ratelimit-unified-overage-status` / `overage-reset`
   - `anthropic-ratelimit-unified-representative-claim`

### Rate limit types

| Type | Key in API response | Description |
|------|-------------------|-------------|
| `five_hour` | `five_hour` | Rolling 5-hour session window |
| `seven_day` | `seven_day` | Rolling 7-day all-models window |
| `seven_day_sonnet` | `seven_day_sonnet` | Rolling 7-day Sonnet-specific window |
| `seven_day_opus` | `seven_day_opus` | Rolling 7-day Opus-specific window |
| `overage` | `extra_usage` | Extra/overage usage (if enabled) |

### API response format

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <oauth_access_token>
anthropic-beta: oauth-2025-04-20
```

```json
{
    "five_hour": {
        "utilization": 35.0,
        "resets_at": "2026-02-06T22:00:00+00:00"
    },
    "seven_day": {
        "utilization": 14.0,
        "resets_at": "2026-02-12T20:00:00+00:00"
    },
    "seven_day_sonnet": {
        "utilization": 39.0,
        "resets_at": "2026-02-09T14:00:00+00:00"
    },
    "seven_day_opus": null,
    "seven_day_oauth_apps": null,
    "seven_day_cowork": null,
    "iguana_necktie": null,
    "extra_usage": {
        "is_enabled": true,
        "monthly_limit": 100000,
        "used_credits": 0.0,
        "utilization": null
    }
}
```

### Authentication

The OAuth token lives at `~/.claude/.credentials.json`:

```json
{
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-...",
        "refreshToken": "sk-ant-ort01-...",
        "expiresAt": 1770412938485,
        "subscriptionType": "team",
        "rateLimitTier": "default_claude_max_5x"
    }
}
```

The access token expires roughly hourly. Claude Code refreshes it automatically — as long as you have an active Claude Code session, the token stays valid for ccusage to read.

### Warning thresholds (from cli.js)

Claude Code shows inline warnings based on these thresholds:

```
five_hour:  90% utilization when 72% of window has passed
seven_day:  75% at 60%, 50% at 35%, 25% at 15%
```

### Other endpoints found in cli.js

- `/api/oauth/profile` — User profile
- `/api/oauth/account/settings` — Account settings
- `/api/claude_code/policy_limits` — Org policy limits (needs `organizationUuid`)
- `/api/organization/claude_code_first_token_date` — Org onboarding date

### Local files Claude Code uses

| File | Written by | Contains |
|------|-----------|----------|
| `~/.claude/.credentials.json` | Claude Code | OAuth tokens, plan tier |
| `~/.claude/stats-cache.json` | Claude Code | Local usage stats (message counts, token counts per model) |
| `~/.claude/usage-limits.json` | ccusage daemon | Cached API usage data (this tool) |
| `~/.claude/statsig/` | Claude Code | Feature flags, experiment assignments |
