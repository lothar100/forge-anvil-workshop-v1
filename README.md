# 🦀 ZeroClaw

**Secure Task Manager Layer with AI Agent Orchestration**

ZeroClaw is an autonomous task management platform that combines a Kanban-style workflow with AI-powered agent execution. It pairs a FastAPI dashboard (**ZeroClaw** on port 9000) with a local LangGraph-based execution engine (**OpenClaw** on port 9100) to dispatch, review, and complete tasks using configurable AI agents backed by [OpenRouter](https://openrouter.ai).

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| **Kanban Dashboard** | Visual board with columns: `pending` → `approved` → `active` → `dev_done` → `review` → `done` |
| **AI Agent Pool** | Pre-seeded agents (Programmer, Architect, Reviewer, Reporter) with configurable models |
| **OpenClaw Engine** | Local LangGraph + OpenRouter runtime for autonomous task execution |
| **Approval Workflow** | HMAC-secured email-based approve/reject flow with configurable TTL |
| **Auto-Critical Detection** | Flags tasks containing security, auth, payment, deploy keywords |
| **Routines** | Cron-scheduled recurring automation tasks |
| **Critiques** | Structured feedback system with severity levels |
| **Action Logs** | Full audit trail of all system events |
| **Email Notifications** | SMTP-based notifications for approvals and task updates |
| **Docker Ready** | Single-command deployment with Docker Compose |

---

## 📐 Architecture

```
┌─────────────────────────────────────────────┐
│                  Browser                    │
│            localhost:9000/dashboard          │
└────────────────────┬────────────────────────┘
                     │
         ┌───────────▼───────────┐
         │     ZeroClaw :9000    │
         │   (FastAPI + Jinja2)  │
         │                       │
         │  • Task CRUD           │
         │  • Agent management    │
         │  • Approval engine     │
         │  • Scheduler/Routines  │
         │  • Critiques & Logs    │
         └───────────┬───────────┘
                     │ HTTP
         ┌───────────▼───────────┐
         │    OpenClaw :9100     │
         │  (LangGraph Runtime)  │
         │                       │
         │  • Job dispatch        │
         │  • LLM execution       │
         │  • Status polling      │
         └───────────┬───────────┘
                     │
         ┌───────────▼───────────┐
         │     OpenRouter API    │
         │  (LLM model gateway)  │
         └───────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- An [OpenRouter API key](https://openrouter.ai/keys)

### Local Setup

```bash
git clone https://github.com/<your-org>/zeroclaw.git
cd zeroclaw

# Configure environment
cp .env.example .env
# Edit .env with your OPENROUTER_API_KEY and SMTP settings

# Create venv and install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Launch
bash run_zeroclaw.sh
```

### Docker

```bash
docker compose up -d --build
```

Open **http://localhost:9000/dashboard** to access the UI.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | *(required)* | API key for OpenRouter LLM access |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server for email notifications |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASS` | — | SMTP password / app password |
| `NOTIFY_EMAIL` | — | Recipient for approval emails |
| `APPROVAL_SECRET` | — | HMAC pepper for approval tokens |
| `OPENCLAW_ENABLED` | `1` | Enable/disable AI agent execution |
| `OPENCLAW_LOCAL_ENABLED` | `1` | Auto-start local OpenClaw server |
| `SCHEDULER_TICK_SECONDS` | `20` | Scheduler poll interval |
| `OPENCLAW_POLL_SECONDS` | `20` | OpenClaw job status poll interval |
| `DASHBOARD_APPROVALS_ENABLED` | `0` | Show approve/reject buttons in UI |
| `AUTO_EMAIL_APPROVAL_ON_CREATE` | `0` | Auto-send approval email on task creation |
| `MODEL_PROGRAMMING` | `openai/gpt-5.2-codex` | Model for Programmer agent |
| `MODEL_ARCHITECTURE` | `anthropic/claude-opus-4.6` | Model for Architect agent |
| `MODEL_REVIEW` | `anthropic/claude-sonnet-4.5` | Model for Reviewer agent |
| `MODEL_REPORTING` | `anthropic/claude-opus-4.5` | Model for Reporter agent |
| `OPENCLAW_JOB_TIMEOUT_SECONDS` | `300` | Timeout for individual AI jobs |

---

## 📁 Project Structure

```
zeroclaw/
├── app/
│   ├── main.py                      # FastAPI app, routes, scheduler
│   ├── db.py                        # SQLite schema, migrations
│   ├── approvals.py                 # HMAC-secured approval system
│   ├── emailer.py                   # SMTP email notifications
│   ├── openclaw.py                  # OpenClaw HTTP client
│   ├── openclaw_local.py            # Local OpenClaw server (:9100)
│   ├── openclaw_langgraph_runtime.py # LangGraph + OpenRouter execution
│   ├── routines.py                  # Cron-based recurring tasks
│   └── templates/                   # Jinja2 HTML templates
│       ├── base.html
│       ├── dashboard.html
│       ├── tasks_new.html
│       ├── task_detail.html
│       ├── agents.html
│       ├── agent_detail.html
│       ├── routines.html
│       ├── critiques.html
│       ├── logs.html
│       └── decision_result.html
├── data/                            # Runtime data (auto-created)
│   ├── zeroclaw.db                  # Main SQLite database
│   ├── openclaw.db                  # OpenClaw job database
│   └── uvicorn.log                  # Server logs
├── docker/
│   └── start_zeroclaw.sh
├── requirements.txt
├── run_zeroclaw.sh                  # Main launcher script
├── run.sh                           # Alias for run_zeroclaw.sh
├── Dockerfile
├── docker-compose.yml
└── .env                             # Environment configuration
```

---

## 🔗 API Endpoints

### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Redirect to dashboard |
| `GET` | `/version` | Build info, health check |
| `GET` | `/status` | System status |
| `GET` | `/dashboard` | Kanban board UI |

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks/new` | New task form |
| `POST` | `/tasks` | Create task |
| `GET` | `/tasks/{id}` | Task detail view |
| `POST` | `/tasks/{id}/update` | Update task |
| `POST` | `/tasks/{id}/delete` | Delete task |
| `POST` | `/tasks/{id}/complete` | Mark complete |
| `POST` | `/api/tasks/move` | Move task between columns |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/agents` | Agent list |
| `POST` | `/agents` | Create agent |
| `GET` | `/agents/{id}` | Agent detail |
| `POST` | `/agents/{id}/update` | Update agent |
| `POST` | `/api/agent_report` | Submit agent report |

### Routines, Critiques & Logs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/routines` | Routines list |
| `POST` | `/routines/create` | Create routine |
| `GET` | `/critiques` | Critiques list |
| `POST` | `/critiques` | Create critique |
| `GET` | `/logs` | Action log viewer |

### Approvals

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/approve?token=...` | Approve via email link |
| `GET` | `/reject?token=...` | Reject via email link |
| `GET` | `/decision/{id}` | Decision result page |

---

## 🗄️ Database

ZeroClaw uses **SQLite** with auto-migration. Core tables:

- **`tasks`** — Task records with status, scheduling, OpenClaw tracking
- **`agents`** — AI agent definitions (name, role, model, active status)
- **`decisions`** — Approval/rejection records with HMAC-secured tokens
- **`critiques`** — Structured feedback with severity levels
- **`action_logs`** — Full audit trail with timestamps and layer tracking

---

## 🔄 Task Lifecycle

```
pending → (approval) → approved → active → dev_done → review → done
              ↓
          rejected
```

1. **Created** — Task enters `pending` status
2. **Approval** — If critical, email sent for human approve/reject
3. **Active** — Dispatched to assigned AI agent via OpenClaw
4. **Dev Done** — Agent completes work, output attached
5. **Review** — Reviewer agent evaluates output (PASS/FAIL)
6. **Done** — Task completed and archived

---

## 🐳 Docker Deployment

```bash
# Build and start
docker compose up -d --build

# Check health
curl http://localhost:9000/version

# View logs
docker compose logs -f zeroclaw
```

---

## 📄 License

MIT

---

<div align="center">
  <sub>Built with FastAPI · LangGraph · OpenRouter</sub>
</div>
