# Agent Zero / ZeroClaw / OpenClaw

> **Agent Zero** — *Forge* — Intelligent Bootstrap Layer

> **Zero Claw** — *Anvil* — Secure Task Manager Layer

> **Open Claw** — *Workshop* — The Office Layer

---

## What Is This

A three-layer autonomous task orchestration platform. You submit work. Agents plan it, execute it, review it, and close it — with human approval gates where it matters.

The stack runs locally on two ports. ZeroClaw (Anvil) manages the board, approvals, scheduling, and pipelines on `:9000`. OpenClaw (Workshop) handles the actual LLM execution via OpenRouter on `:9100`. Agent Zero (Forge) is the bootstrap layer — Claude Code acting as the intelligent entry point that interprets your intent, routes tasks, and bridges the gap between you and the system.

---

## Architecture

```
 YOU
  │
  │  /zero, /status, /task, natural language
  │
  ▼
┌──────────────────────────────────────────────────────┐
│           AGENT ZERO  (Forge)                        │
│           Claude Code CLI                            │
│                                                      │
│  • Interprets user intent                            │
│  • Routes tasks via slash commands                   │
│  • Direct Claude CLI passthrough (/zero:claude)      │
│  • Monitors system health (/status)                  │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP (localhost:9000)
                       ▼
┌──────────────────────────────────────────────────────┐
│           ZEROCLAW  (Anvil)  :9000                   │
│           FastAPI + Jinja2 + SQLite                  │
│                                                      │
│  • Kanban dashboard (task board)                     │
│  • Pipeline executor (block-based execution engine)  │
│  • Agent management (roles, files, pipelines)        │
│  • HMAC-secured email approval workflow              │
│  • Scheduler (cron, interval, recurring)             │
│  • Routines engine (automated background tasks)      │
│  • Claude CLI health state machine                   │
│  • Critiques, action logs, audit trail               │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP (localhost:9100)
                       ▼
┌──────────────────────────────────────────────────────┐
│           OPENCLAW  (Workshop)  :9100                 │
│           LangGraph + OpenRouter                     │
│                                                      │
│  • Job dispatch and execution                        │
│  • LLM calls via OpenRouter (multi-model)            │
│  • Job status tracking (queued/running/done/failed)  │
│  • Token-authenticated API                           │
│  • Isolated execution (no framework leaks)           │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
              OpenRouter API (LLM gateway)
              + Claude CLI (escalation path)
```

---

## Task Lifecycle

A task moves through the board like this:

```
pending ──► approved ──► active ──► dev_done ──► review ──► done
   │            │                      │
   ▼            ▼                      ▼
rejected    blocked              (FAIL → retry)
            paused_limit
            queued_for_claude
```

**Step by step:**

1. **Created** — Task enters `pending`. If it contains critical keywords (security, auth, payment, deploy, etc.), it's auto-flagged and an approval email is sent.
2. **Approval** — HMAC-secured email with approve/reject links. 72-hour TTL. The scheduler syncs decision status every tick.
3. **Dispatched** — The scheduler picks up approved tasks and hands them to the pipeline executor, which runs in a background thread.
4. **Pipeline Execution** — The pipeline walks through its blocks in sequence:
   - **executor** — Sends work to OpenRouter (via OpenClaw) or Claude CLI
   - **review** — Evaluates the output, returns PASS or FAIL verdict
   - **retry** — Re-runs with review feedback (configurable max retries)
   - **escalate** — Falls back to Claude CLI if OpenRouter fails
   - **done** — Finalizes the task
5. **Dev Done** — Output is stored. If the review routine is enabled, a review task is auto-created.
6. **Review** — A reviewer agent evaluates the output. PASS moves the source task to `done`. FAIL sends it back for another attempt.
7. **Done** — Task is complete.

If Claude CLI hits rate limits or daily caps, tasks are paused (`paused_limit`) or queued (`queued_for_claude`) and automatically resume when the CLI recovers.

---

## Agents

Four agents are seeded by default. Each has a role, a model, and a set of markdown files that define their personality and instructions.

| Agent | Role | Default Model | Purpose |
|-------|------|---------------|---------|
| **Programmer** | programming | openai/gpt-5.2 | Write code, implement features |
| **Architect** | architecture | openai/gpt-5.2 | Design systems, plan structure |
| **Reviewer** | reviewing | openai/gpt-5.2 | Evaluate output, PASS/FAIL verdicts |
| **Reporter** | reporting | openai/gpt-5.2 | Summarize, generate reports |

Each agent has a directory at `data/agents/{name}/` containing:
- **SOUL.md** — Personality and values
- **INSTRUCTIONS.md** — Role-specific behavior and output format
- **CONTEXT.md** — Project context and conventions
- Custom `.md` files — Additional context loaded at execution time

All files are concatenated into the agent's system prompt when the pipeline runs.

---

## Pipeline Executor

Pipelines are stored as JSON arrays of blocks. Each agent can have a custom pipeline, or fall back to the default. The executor walks through blocks sequentially:

| Block | What It Does |
|-------|--------------|
| `executor` | Run the task via OpenRouter or Claude CLI. The core work block. |
| `review` | Send the previous output to a reviewer model. Parse PASS/FAIL. |
| `retry` | Re-run with failure context and review notes. Respects `max_retries`. |
| `escalate` | Escalate to Claude CLI when OpenRouter can't handle it. |
| `route` | Conditional branching (evaluate a condition to choose a path). |
| `done` | Terminal block. Finalize and store output. |

The executor logs every block execution to the `executor_log` table with timing, model used, pass/fail status, and error details.

---

## Claude CLI Health

The system tracks Claude CLI availability with a state machine:

```
HEALTHY ──► DEGRADED (rate limits) ──► DAILY_LIMIT_HIT (quota exhausted)
   │                                         │
   ▼                                    auto-reset at midnight
UNAVAILABLE (5+ consecutive failures)
   │
   ▼ (30-min cooldown)
HEALTHY

AUTH_FAILED ──► requires manual reset
```

When Claude CLI is unhealthy, pipeline blocks with `on_limit` config can `stop` (pause the pipeline with resume index), `queue` (queue for later), or `fallback` (skip and continue).

Paused and queued tasks automatically resume when health returns to HEALTHY.

---

## Routines

Background automation that ticks every 10 seconds:

| Routine | Purpose |
|---------|---------|
| **idle_autostart** | Auto-dispatch the next approved task when an agent is idle |
| **review_autocreate** | Auto-create a review task when a task reaches `dev_done` |
| **status_report_email** | Email a status summary after N completions or on a timer |

Routines are toggled on/off from the dashboard and can be scoped to specific agents.

---

## Slash Commands

These commands are typed to Agent Zero (Claude Code) for direct interaction with the system:

| Command | Action |
|---------|--------|
| `/zero {prompt}` | Submit task directly to ZeroClaw. No interpretation. Returns task ID + link. |
| `/zero:plan {prompt}` | Submit as a planning task, assigned to the Architect agent. |
| `/zero:code {prompt}` | Submit as a programming task, assigned to the Programmer agent. |
| `/zero:claude {prompt}` | Run directly via Claude CLI. No ZeroClaw, no pipeline. Raw passthrough. |
| `/zero:{agent} {prompt}` | Submit task assigned to a named agent (case-insensitive lookup). |
| `/status` | Dashboard summary — task counts by status, paused/queued, Claude health. |
| `/task {id}` | Fetch and display a specific task's details, output, and execution log. |

---

## Quick Start

### Prerequisites
- Python 3.11+
- [OpenRouter API key](https://openrouter.ai/keys)

### Local Setup

```bash
git clone <repo-url>
cd forge-anvil-workshop-v1

cp .env.example .env
# Edit .env — set OPENROUTER_API_KEY and SMTP settings

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

bash run_zeroclaw.sh
```

### Docker

```bash
docker compose up -d --build
```

Open **http://localhost:9000/dashboard**.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | *(required)* | OpenRouter LLM access |
| `APPROVAL_SECRET` | — | HMAC pepper for approval tokens |
| `PUBLIC_BASE_URL` | `http://localhost:9000` | Base URL for email approval links |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASS` | — | SMTP password / app password |
| `APPROVER_EMAIL` | — | Recipient for approval emails |
| `OPENCLAW_ENABLED` | `1` | Enable AI agent execution |
| `OPENCLAW_LOCAL_ENABLED` | `1` | Auto-start local OpenClaw sidecar |
| `CLAUDE_CLI_ENABLED` | `1` | Enable Claude CLI escalation path |
| `CLAUDE_CLI_TIMEOUT_SECONDS` | `300` | Claude CLI subprocess timeout |
| `SCHEDULER_TICK_SECONDS` | `20` | Task scheduler poll interval |
| `OPENCLAW_POLL_SECONDS` | `20` | OpenClaw job status poll interval |
| `ROUTINES_TICK_SECONDS` | `10` | Routines tick interval |
| `DASHBOARD_APPROVALS_ENABLED` | `0` | Allow approve/reject from the dashboard UI |
| `AUTO_EMAIL_APPROVAL_ON_CREATE` | `true` | Auto-send approval email for critical tasks |
| `AUTO_CRITICAL_KEYWORDS` | `security,auth,login,...` | Keywords that auto-flag tasks as critical |
| `APPROVAL_TTL_HOURS` | `72` | Hours before approval links expire |
| `SUMMARY_EMAIL_EVERY_MINUTES` | `360` | Summary email interval (0 to disable) |

---

## Project Structure

```
forge-anvil-workshop-v1/
├── app/
│   ├── main.py                       # FastAPI routes, scheduler, startup
│   ├── db.py                         # SQLite schema + migrations
│   ├── pipeline_executor.py          # Block-based pipeline execution engine
│   ├── claude_executor.py            # Claude CLI subprocess executor
│   ├── claude_health.py              # Claude CLI health state machine
│   ├── approvals.py                  # HMAC-secured decision/approval system
│   ├── emailer.py                    # SMTP email notifications
│   ├── openclaw.py                   # OpenClaw HTTP client (dispatch + poll)
│   ├── openclaw_local.py             # Local OpenClaw server (:9100)
│   ├── openclaw_langgraph_runtime.py # LangGraph + OpenRouter execution
│   ├── routines.py                   # Background automation routines
│   ├── agent_files.py                # Agent markdown file management
│   └── templates/                    # Jinja2 HTML templates
│       ├── base.html
│       ├── dashboard.html
│       ├── tasks_new.html
│       ├── task_detail.html
│       ├── agents.html
│       ├── agent_detail.html
│       ├── pipelines.html
│       ├── routines.html
│       ├── critiques.html
│       ├── logs.html
│       └── decision_result.html
├── data/                             # Runtime data (auto-created)
│   ├── zeroclaw.db                   # Main SQLite database
│   ├── openclaw.db                   # OpenClaw job database
│   └── agents/                       # Per-agent markdown files
│       ├── Programmer/
│       ├── Architect/
│       ├── Reviewer/
│       └── Reporter/
├── docker/
│   └── start_zeroclaw.sh
├── run_zeroclaw.sh                   # Main launcher
├── supervisor_start_zeroclaw.sh
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env
```

---

## API Endpoints

### Dashboard & Tasks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dashboard` | Kanban board UI |
| `POST` | `/tasks` | Create task (form data) |
| `GET` | `/tasks/{id}` | Task detail view |
| `POST` | `/tasks/{id}/update` | Update task fields |
| `POST` | `/tasks/{id}/delete` | Delete task |
| `POST` | `/tasks/{id}/complete` | Resend approval email |
| `POST` | `/api/tasks/move` | Move task between columns (JSON) |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/agents` | Agent list with running counts |
| `POST` | `/agents` | Create agent |
| `GET` | `/agents/{id}` | Agent detail with files |
| `POST` | `/agents/{id}/update` | Update agent config |
| `POST` | `/agents/{id}/files/{name}` | Save agent markdown file |

### Pipelines

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines` | Pipeline list/editor |
| `POST` | `/pipelines/create` | Create pipeline |
| `POST` | `/pipelines/{id}/update` | Update pipeline blocks |

### Approvals & Decisions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/approve?decision_id=...&token=...` | Approve via email link |
| `GET` | `/reject?decision_id=...&token=...` | Reject via email link |
| `GET` | `/decision/{id}` | Decision detail page |

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/version` | Build ID, PID, working directory |
| `GET` | `/api/claude-health` | Claude CLI health status (JSON) |
| `POST` | `/api/claude-health/reset` | Manual health reset |
| `GET` | `/logs` | Action log viewer |
| `GET` | `/critiques` | Critiques list |
| `GET` | `/routines` | Routines management |

---

## Database

SQLite with auto-migration. Core tables:

| Table | Purpose |
|-------|---------|
| `tasks` | Task records — status, scheduling, execution results, OpenClaw tracking |
| `agents` | Agent definitions — name, role, model, pipeline assignment |
| `pipelines` | Pipeline configurations — block-based execution sequences (JSON) |
| `decisions` | Approval records — HMAC tokens, expiry, decider metadata |
| `executor_log` | Per-block execution history — timing, model, pass/fail, errors |
| `claude_health` | Claude CLI health state (singleton row) |
| `routines` | Background routine definitions |
| `routine_state` | KV store for routine persistence |
| `critiques` | Structured feedback with severity levels |
| `action_logs` | Full audit trail — timestamps, layers, models |

---

## License

MIT

---

<div align="center">
  <sub>Forge &middot; Anvil &middot; Workshop</sub>
</div>
