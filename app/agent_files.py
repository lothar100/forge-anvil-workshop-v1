"""Agent Internal Files — manages SOUL.md, INSTRUCTIONS.md, CONTEXT.md per agent.

Each agent gets a directory at ``data/agents/{agent_name}/`` containing
markdown files that define personality, instructions, and persistent context.
"""
from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
AGENTS_DIR = DATA_DIR / "agents"

# Standard file names
STANDARD_FILES = ("SOUL.md", "INSTRUCTIONS.md", "CONTEXT.md")

# ---------------------------------------------------------------------------
# Default content generators
# ---------------------------------------------------------------------------

_SOUL_TEMPLATE = """\
# {name} — Soul

You are **{name}**, a {role} agent in the ZeroClaw autonomous task system.

## Personality
- Professional and focused
- Clear and concise in communication
- Thorough in your work

## Values
- Accuracy over speed
- Completeness over brevity when it matters
- Always explain your reasoning
"""

_INSTRUCTIONS_TEMPLATE = """\
# {name} — Instructions

## Role
You are the **{role}** agent. Your primary responsibilities:

{role_instructions}

## Output Format
- Return your output in markdown
- Include a short "Result" section first with a summary
- Be specific and actionable
"""

_CONTEXT_TEMPLATE = """\
# {name} — Context

## Project Context
This agent operates within the ZeroClaw task management system.

## Conventions
- Follow existing code patterns and project conventions
- Use the tech stack already established in the project
"""

_ROLE_INSTRUCTIONS = {
    "programming": (
        "- Write clean, well-structured code\n"
        "- Include full file paths and complete code blocks\n"
        "- Handle edge cases and error conditions\n"
        "- Follow existing project patterns and conventions"
    ),
    "architecture": (
        "- Make high-level design decisions\n"
        "- Identify tradeoffs between approaches\n"
        "- Create concrete implementation plans\n"
        "- Consider scalability, maintainability, and security"
    ),
    "reviewing": (
        "- Thoroughly review code and deliverables\n"
        "- Identify bugs, issues, and risks\n"
        "- Propose specific fixes and improvements\n"
        "- Give a clear PASS or FAIL verdict"
    ),
    "reporting": (
        "- Summarize work clearly and concisely\n"
        "- Highlight key findings and next steps\n"
        "- Use structured formatting for readability\n"
        "- Include metrics where available"
    ),
}


def _default_content(filename: str, name: str, role: str) -> str:
    role_key = role.lower().strip()
    if filename == "SOUL.md":
        return _SOUL_TEMPLATE.format(name=name, role=role)
    if filename == "INSTRUCTIONS.md":
        role_instr = _ROLE_INSTRUCTIONS.get(role_key, "- Complete tasks as assigned\n- Be thorough and accurate")
        return _INSTRUCTIONS_TEMPLATE.format(name=name, role=role, role_instructions=role_instr)
    if filename == "CONTEXT.md":
        return _CONTEXT_TEMPLATE.format(name=name)
    return f"# {name} — {filename}\n\n(Custom file)\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_agent_dir(name: str, role: str = "general") -> Path:
    """Create agent directory and default files if they don't exist."""
    agent_dir = AGENTS_DIR / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    for fname in STANDARD_FILES:
        fpath = agent_dir / fname
        if not fpath.exists():
            fpath.write_text(_default_content(fname, name, role), encoding="utf-8")
    return agent_dir


def list_agent_files(name: str) -> list[str]:
    """Return list of markdown files for the agent."""
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return []
    return sorted(f.name for f in agent_dir.glob("*.md"))


def read_agent_file(name: str, filename: str) -> str:
    """Read an agent's markdown file."""
    fpath = AGENTS_DIR / name / filename
    if not fpath.exists():
        return ""
    return fpath.read_text(encoding="utf-8", errors="replace")


def write_agent_file(name: str, filename: str, content: str) -> None:
    """Write/update an agent's markdown file."""
    agent_dir = AGENTS_DIR / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    fpath = agent_dir / filename
    fpath.write_text(content, encoding="utf-8")


def delete_agent_file(name: str, filename: str) -> bool:
    """Delete a custom agent file. Won't delete standard files."""
    if filename in STANDARD_FILES:
        return False
    fpath = AGENTS_DIR / name / filename
    if fpath.exists():
        fpath.unlink()
        return True
    return False


def rename_agent_dir(old_name: str, new_name: str) -> None:
    """Rename an agent's directory when the agent name changes."""
    old_dir = AGENTS_DIR / old_name
    new_dir = AGENTS_DIR / new_name
    if old_dir.is_dir() and not new_dir.exists():
        old_dir.rename(new_dir)
