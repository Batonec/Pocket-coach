# Coach MCP

MCP server over the Trainer mini-app data, plus tools to **debug the
next-workout recommendations** and manage the **coaching state** (preparation
phase, waist measurements, injection-cycle config). It reads the same SQLite
database the backend uses (`backend`) and reuses `recommender.py` /
`prompt_builder.py` / `coach_state.py` / `coach_features.py`, so what you see
here is exactly what the app's backend generates.

Use it to chat with Claude as a "coach" about your training, and to inspect the
recommendation pipeline (the exact prompt, the model's attempts with semantic-
validator violations and the auto-reprompt, token usage and cost).

## Tools

### Data (read-only)

| Tool | What it does |
|------|--------------|
| `coach_list_workouts(limit=20)` | Workout history (newest first) + the compact serialization the model sees |
| `coach_get_workout(workout_id)` | One workout, full payload |
| `coach_list_body_weights()` | Body-weight history |
| `coach_list_waists()` | Waist-measurement history (cm) |
| `coach_get_catalog()` | The exercise catalog (only these exercises exist) |
| `coach_get_state()` | Coaching state: phase + params, block week, weekly volume target, return-from-break flag, hormone-cycle day |
| `coach_list_events()` | Events — gaps in training with a reason (newest first); `end_date: null` marks the one that is still running |

### Coaching state (writing)

| Tool | What it does |
|------|--------------|
| `coach_set_phase(phase, params?)` | Switch the preparation phase by hand (`cut_recomp` / `lean_bulk` / `maintenance`); stamps today as the phase start. No automatic switching ever — when a phase goal is reached, the prompt only asks the model to *suggest* the switch |
| `coach_update_state(waist_limit_cm?, waist_base_cm?)` | Global knobs: hard waist limit, phase-base waist |
| `coach_update_profile(block, text?)` | Replace one profile block (empty text deletes it); previous file kept as a timestamped `.bak` |
| `coach_add_waist(waist_cm, entry_date?)` | Record a waist measurement (upserts per date) |
| `coach_delete_waist(entry_id)` | Remove a mistyped measurement |
| `coach_add_event(text, start_date?, end_date?)` | Record an event — a gap in training with a reason ("was ill", "business trip"). No end date means it is still running, and only one event may be open; future dates are rejected |
| `coach_update_event(event_id, text?, start_date?, end_date?)` | Edit an event; omitted fields keep their current value, so "it ended yesterday" is one call with `end_date`. Pass `end_date=""` to reopen it |
| `coach_delete_event(event_id)` | Remove an event recorded by mistake |

### Recommendation engine

| Tool | What it does |
|------|--------------|
| `coach_get_stored_recommendation()` | The recommendation currently cached for the app (status/based_on/payload/tokens/stale) |
| `coach_preview_prompt(limit=20)` | The exact system+user prompt and JSON schema — **no API call** (free). Includes phase, block week and cycle info |
| `coach_debug_recommendation(limit=20)` | Full generation run with the semantic validator: every attempt (raw output + violations + reprompt), final result, tokens/cost. Does not write to the DB |
| `coach_generate_recommendation(limit=20, store=false)` | Generate a validated recommendation; `store=true` overwrites the app's cached recommendation |
| `coach_weekly_report(days=7, fresh=false)` | Coach-style weekly retrospective (Markdown): week totals vs targets, PRs, weight/waist trends, discipline, next-week focus. Always covers the last **closed** calendar week (Mon–Sun), served from the cache instantly (a Monday-midnight timer pre-generates it); `fresh=true` regenerates for tokens |
| `coach_phase_summary(history_index?)` | What a preparation phase delivered: duration, sessions + frequency, weight/waist start→finish with rate, PRs, discipline. No args — the current phase; an index — a closed phase from the journal |
| `coach_costs()` | Monthly Claude API spend: recommendation generations + weekly reports (calls, tokens, estimated USD) |

All tools accept an optional `user_id` (defaults to the configured user).

## State files (next to the DB)

- `coach_profile.json` — athlete prose profile (personal/medical context; never
  in the repo, template: `backend/examples/coach_profile.example.json`);
- `coach_state.json` — structured coaching state: phase, phase start, per-phase
  overrides, waist limit/base, injection day (template:
  `backend/examples/coach_state.example.json`; override path with `COACH_STATE_PATH`).

## Environment

| Var | Default | Notes |
|-----|---------|-------|
| `ANTHROPIC_API_KEY` | — | Required for `coach_debug_recommendation` / `coach_generate_recommendation` |
| `COACH_MCP_BACKEND_DIR` | `../backend` | Backend root holding the `trainer/` package. On the VPS: `/opt/trainer-miniapp/app` |
| `MINIAPP_DB_PATH` | `<backend_dir>/data/trainer.db` | SQLite path. On the VPS: `/opt/trainer-miniapp/data/trainer.db` |
| `COACH_MCP_STATIC_DIR` | `MINIAPP_STATIC_DIR` or `<backend_dir>/web` | Holds `data/exercises.json`. On the VPS: `/opt/trainer-miniapp/www` |
| `COACH_MCP_USER_ID` | `MINIAPP_TELEGRAM_RECOVERY_USER_ID` or `3` | Which user to operate on |
| `ANTHROPIC_MODEL` | from `recommender` (`claude-opus-5`) | Model for generation |
| `COACH_MCP_HOST` / `COACH_MCP_PORT` | `127.0.0.1` / `8001` | streamable-http bind (8001 to avoid investor-mcp's 8000) |
| `COACH_MCP_PATH` | `/mcp` | HTTP path; use a secret path in production |
| `COACH_MCP_AUTH_TOKEN` | — | If set, require `Authorization: Bearer <token>` |
| `COACH_MCP_ALLOWED_HOSTS` | — | Comma list → enables strict DNS-rebinding protection |

## Run locally (stdio, e.g. Claude Desktop)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r coach_mcp/requirements.txt
ANTHROPIC_API_KEY=sk-ant-... python coach_mcp/server.py
```

## Deploy on the VPS (streamable-http behind Cloudflare tunnel)

Same shape as `investor-mcp`. The backend already lives at
`/opt/trainer-miniapp/app`, so point the importer there and reuse the existing
`backend.env` secrets.

```bash
# one-time
python3 -m venv /opt/coach-mcp/venv
/opt/coach-mcp/venv/bin/pip install -r requirements.txt
# env (own EnvironmentFile, or reuse the backend's):
#   COACH_MCP_BACKEND_DIR=/opt/trainer-miniapp/app
#   MINIAPP_DB_PATH=/opt/trainer-miniapp/data/trainer.db
#   COACH_MCP_STATIC_DIR=/opt/trainer-miniapp/www
#   ANTHROPIC_API_KEY=...           (already in /etc/trainer-miniapp/backend.env)
#   COACH_MCP_PATH=/<random-secret-path>/mcp
/opt/coach-mcp/venv/bin/python server.py --transport streamable-http --host 127.0.0.1 --port 8001
```

Then add a Cloudflare tunnel public hostname → `http://localhost:8001` and use
`https://<host>/<secret-path>/mcp` as the connector URL in Claude.

> **RAM note:** the VPS is ~1 GB and already runs the backend, two Caddy
> containers, cloudflared and the investor-mcp tunnel. A second `mcp`+uvicorn
> process adds ~50–80 MB — check `free -m` headroom (or run it on demand) before
> leaving it always-on.

> **Security:** these tools expose the user's full training history and can spend
> Anthropic tokens (`coach_debug_recommendation` / `coach_generate_recommendation`).
> Behind a public tunnel, use a secret `COACH_MCP_PATH` and/or `COACH_MCP_AUTH_TOKEN`.
