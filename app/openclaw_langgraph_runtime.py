from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph


class LGState(TypedDict, total=False):
    output: str


def _role_system_prompt(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("programmer", "programming", "implementation", "developer"):
        return (
            "You are an expert software engineer agent. "
            "Produce precise, implementation-ready output. "
            "When code is required, include full code blocks and file paths."
        )
    if r in ("report", "reporting", "writer"):
        return "You are a concise technical reporting agent. Provide a clear summary and next steps."
    if r in ("review", "reviewer"):
        return "You are a strict reviewer. Point out issues, risks, and propose fixes."
    if r in ("architecture", "architect"):
        return "You are a senior software architect. Provide decisions, tradeoffs, and a concrete plan."
    return "You are a helpful autonomous agent. Provide accurate, actionable output."


def run_job_langgraph(*, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a job using LangGraph + an OpenRouter-backed chat model.

    Expects payload schema:
      { task:{title,description}, agent:{role,model,name}, openrouter_api_key:str, metadata:{...} }

    Returns: {ok:bool, output:str, error:str}
    """

    task = (payload.get("task") or {}) if isinstance(payload, dict) else {}
    agent = (payload.get("agent") or {}) if isinstance(payload, dict) else {}
    meta = (payload.get("metadata") or {}) if isinstance(payload, dict) else {}

    title = str(task.get("title") or "(untitled)")
    desc = str(task.get("description") or "")

    role = str(agent.get("role") or meta.get("role") or "")
    model = str(agent.get("model") or meta.get("model") or meta.get("openrouter_model") or "").strip()
    if not model:
        model = "openai/gpt-4o-mini"

    api_key = str(payload.get("openrouter_api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "openrouter_api_key_missing"}

    # OpenRouter is OpenAI-compatible
    base_url = str(meta.get("openrouter_base_url") or "https://openrouter.ai/api/v1")
    app_url = str(meta.get("openrouter_app_url") or "http://localhost:9000")
    app_name = str(meta.get("openrouter_app_name") or "ZeroClaw/OpenClaw")

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=120,
        default_headers={
            "HTTP-Referer": app_url,
            "X-Title": app_name,
        },
    )

    sys = _role_system_prompt(role)
    user = (
        f"Task Title: {title}\n\n"
        f"Task Description:\n{desc}\n\n"
        "Return your output in markdown. Include a short 'Result' section first."
    )

    def llm_node(state: LGState) -> LGState:
        resp = llm.invoke([SystemMessage(content=sys), HumanMessage(content=user)])
        return {"output": str(resp.content)}

    g = StateGraph(LGState)
    g.add_node("llm", llm_node)
    g.set_entry_point("llm")
    g.add_edge("llm", END)
    app = g.compile()

    try:
        out = app.invoke({})
        return {"ok": True, "output": str(out.get("output") or ""), "used_model": model}
    except Exception as e:
        return {"ok": False, "error": f"langgraph_runtime_failed: {e}"}
