# Forge/Anvil/Workshop — Architecture Audit

**Date:** 2026-02-22
**Scope:** All files in `app/`, plus configuration and Docker files

---

## 1. Where Do LLM Calls Happen?

All LLM calls flow through a single path:

| File | Function | What It Does |
|------|----------|--------------|
| `app/openclaw_langgraph_runtime.py` | `run_job_langgraph()` | **The only function that calls an LLM.** Uses `ChatOpenAI` via OpenRouter's OpenAI-compatible API. Builds a single-node LangGraph workflow with a role-specific system prompt + task content as the user message. |
| `app/openclaw_local.py` | `_run_job()` | Calls `run_job_langgraph()` inside a thread with a timeout wrapper. Does NOT make LLM calls itself. |
| `app/openclaw.py` | `dispatch_job()` | HTTP client — sends a POST to the local OpenClaw server at `:9100/jobs`. No LLM calls. |

**Full call chain:**
```
main.py scheduler (_tick_scheduled_tasks)
  → openclaw.dispatch_job()         [HTTP POST to :9100]
    → openclaw_local.create_job()   [queues job, spawns thread]
      → openclaw_local._run_job()   [thread wrapper with timeout]
        → openclaw_langgraph_runtime.run_job_langgraph()  [ACTUAL LLM CALL]
```

**The same chain is used by routines:**
```
routines.tick_routines()
  → routines.dispatch_one_task_row()
    → openclaw.dispatch_job()       [same HTTP POST to :9100]
      → ... same path as above
```

**Verdict:** LLM calls are properly centralized in `openclaw_langgraph_runtime.py`. No inline LLM calls exist in route handlers, the scheduler, or routines.

---

## 2. Does the Scheduler Properly Dispatch All Work Through OpenClaw?

**Yes.** The scheduler in `main.py` has two relevant jobs:

1. **`_tick_scheduled_tasks()`** — Finds approved tasks and calls `openclaw.dispatch_job()` to send them to the OpenClaw server. It never constructs prompts or calls LLMs directly.

2. **`_poll_openclaw_jobs()`** — Polls job status from OpenClaw via `openclaw.get_status()`. Processes results (updates task status, handles review/resolve logic). No LLM calls.

The routines system (`routines.py`) also dispatches through `dispatch_one_task_row()` → `openclaw.dispatch_job()`.

**No code path in `main.py` or `routines.py` makes direct LLM calls.**

---

## 3. Is There Any Task Execution Logic That Bypasses OpenClaw?

**No bypasses found.** Every path that executes a task goes through the OpenClaw dispatch chain:

- `main.py:_tick_scheduled_tasks()` → `openclaw.dispatch_job()`
- `routines.py:dispatch_one_task_row()` → `openclaw.dispatch_job()`

There are no inline prompt constructions, no direct `ChatOpenAI` instantiations, and no API calls to OpenRouter outside of `openclaw_langgraph_runtime.py`.

**However, there is tightly coupled orchestration logic in `main.py` and `routines.py`:**
- Review task creation, resolve task creation, and planning task creation all happen in `routines.py` — these create tasks with specific prompts embedded in descriptions, but they correctly dispatch through OpenClaw for execution.
- The `_poll_openclaw_jobs()` function in `main.py` contains complex post-processing logic (review verdict parsing, resolve application, task status transitions) that is not LLM execution but is business logic that could benefit from being extracted.

---

## 4. Are Agent Definitions in the DB Actually Used for Dispatch?

**Yes, but with caveats:**

- The `agents` table has `model` and `role` columns
- When dispatching, the agent's `model` is passed to OpenClaw in the payload: `agent={'model': agent_row['model'], 'role': agent_row['role'], ...}`
- `openclaw_langgraph_runtime.py` uses `agent.model` to select the LLM and `agent.role` to select the system prompt
- The agent's role maps to one of 5 hardcoded system prompts in `_role_system_prompt()`

**Issues found:**
1. **Hardcoded agent IDs in routines:** `routines.py` assumes `agent_id=2` is the architect (lines 734, 901). If agents are deleted/reordered, this breaks.
2. **Hardcoded agent names in parsing:** `_parse_routine_prompt()` maps "ada"→1, "jorven"→2, "iris"→3, "quimby"→4 (line 517).
3. **Model field on agents is decorative for routines:** The `idle_autostart` routine and `review_autocreate` routine pass the agent's model through, but the model could also come from env vars (`MODEL_PROGRAMMING`, etc.) — there's some ambiguity about which takes precedence.

---

## 5. Does the Approval Workflow Properly Gate Task Execution?

**Yes, with one configuration-dependent gap:**

- Tasks with `is_critical=1` get `requires_approval=1` and stay in `pending` until approved via email link
- The scheduler (`_tick_scheduled_tasks`) only dispatches tasks in `approved` status
- Board drag-drop (`api_tasks_move`) enforces: `pending→active` is blocked if `requires_approval=1`
- Form updates (`task_update`) block moving to `approved`/`rejected` via form if task requires approval

**Configuration gap:**
- The `idle_autostart` routine in `routines.py` (Step 3) auto-approves ALL non-critical pending tasks:
  ```python
  pending_noncrit = con.execute("SELECT * FROM tasks WHERE status='pending' AND is_critical=0")
  ```
  This means when `idle_autostart` is enabled, non-critical tasks bypass any manual approval entirely. This is by design (automation), but worth noting.

---

## 6. Additional Findings

### Architecture Compliance (Forge → Anvil → Workshop)
The suspected issue — Agent Zero (Forge) doing direct LLM work — is **not present in this codebase**. Agent Zero / Forge is not implemented in this repository. This codebase contains only ZeroClaw (Anvil) and OpenClaw (Workshop). All LLM execution properly flows through the OpenClaw layer.

### Code Quality Issues
1. **Inline `import re`** in multiple places within `_poll_openclaw_jobs()` (lines 1027, 1036, 1075)
2. **Duplicate `FastAPI` assignment** in `openclaw_local.py` (lines 148-151)
3. **No `description` column** in the routines seed despite being used in `routines_create()` — the schema has it in `_parse_routine_prompt()` but `ensure_default_routine()` doesn't include it
4. **No `retry_count` column** in the initial schema — it's added via migration but not in `init_db()`
5. **No `review_summary` column** in the initial schema — same migration gap
6. **`action_logs` table** missing `layer` and `model` columns in `init_db()` but used throughout `main.py`

### Missing Features for v2
- No pipeline abstraction — execution is 1:1 (one task → one LLM call)
- No multi-model execution chain (e.g., first attempt → review → retry → escalate)
- No Claude CLI integration
- No health monitoring for executors
- Agent identity is minimal (name + role + model) — no personality/instruction files
