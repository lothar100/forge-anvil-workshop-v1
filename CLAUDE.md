# ZeroClaw Project Instructions

## Architecture

```
Agent Zero (Forge) — Minimax M2.5 via external tool, connects to OpenClaw
  ↓ should forward /zero commands to Claude CLI
Claude Code / CLI (this layer) — orchestration, command routing
  ├→ HTTP to ZeroClaw :9000 — task submission, status queries
  └→ Direct execution only for /zero:claude
ZeroClaw (Anvil) :9000 — task board, pipelines, approvals, scheduling
  └→ OpenClaw (Workshop) :9100 — LLM execution via OpenRouter (Minimax, etc.)
```

## Critical: /zero Command Behavior

When you receive a `/zero` command (whether from Agent Zero or typed directly), you MUST forward work to the ZeroClaw service. You are an orchestration layer. You do NOT execute tasks yourself.

### Rules

1. **NEVER attempt to fulfill a /zero task yourself.** Your job is to POST to ZeroClaw, not to write code, plan architecture, or generate reports.
2. **If the ZeroClaw service (localhost:9000) is unreachable, start it.** Run `python start.py` to bring services up. If that fails, tell the user. Do NOT fall back to doing the work yourself.
3. **If the HTTP POST fails for any reason, report the error.** Do not silently retry via other means or attempt the task directly.
4. **Do not use A2A or any other protocol as a fallback.** The only path for /zero commands is the ZeroClaw HTTP API.

### Command Reference

| Command | Action |
|---------|--------|
| `/zero {prompt}` | POST to `http://localhost:9000/tasks` (form-encoded: title, description) |
| `/zero:code {prompt}` | Same, but assign to Programmer agent (look up ID first) |
| `/zero:plan {prompt}` | Same, but assign to Architect agent (look up ID first) |
| `/zero:claude {prompt}` | Run `claude -p "{prompt}"` directly — no ZeroClaw |
| `/zero:{agentName} {prompt}` | Look up agent by name, submit task with their ID |
| `/status` | Query ZeroClaw dashboard or DB for task/health summary |
| `/task {id}` | Query task detail from ZeroClaw |

### Task Creation Details

- Endpoint: `POST http://localhost:9000/tasks`
- Content-Type: `application/x-www-form-urlencoded`
- Fields: `title` (first line or first 80 chars), `description` (full prompt), `assigned_agent_id` (optional)
- Returns 303 redirect — get new task ID with: `SELECT id FROM tasks ORDER BY id DESC LIMIT 1`
- Agent lookup: `SELECT id, name, role FROM agents WHERE LOWER(name)=LOWER(?)`
- DB path: `data/zeroclaw.db`

## Bootstrap & Starting Services (IMPORTANT — do this first)

Before any /zero command will work, ZeroClaw must be running. The typical bootstrap flow is:

### With a zip file (full deploy)
```bash
python start.py --zip path/to/code.zip    # Extract code + start services
```
This extracts the zip into the project directory (auto-detects nested folders), restarts services if they were already running, then starts everything up.

### Without a zip (just start services)
```bash
python start.py           # Start both services (background)
python start.py --check   # Check if services are up
python start.py --stop    # Stop services
```

Or use the `/bootstrap` command in Claude Code (supports both modes — with or without a zip path).

ZeroClaw runs on port 9000. OpenClaw auto-starts on port 9100 with it.
If the service is down when a /zero command arrives, tell the user to run `python start.py`.
