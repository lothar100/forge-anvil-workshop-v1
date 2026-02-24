# Auto-generated seed data - DO NOT EDIT MANUALLY
# Run: python -c "from app.seed_data import seed_all; seed_all()"

from datetime import datetime
from app.db import connect
import json
import os


def seed_pipelines():
    """Seed pipelines table with all pipeline configurations."""
    con = connect()
    cur = con.cursor()

    # Clear existing pipelines
    cur.execute("DELETE FROM pipelines")

    pipelines_data = [
        {
                "id": 1,
                "name": "Programming Pipeline",
                "description": "",
                "task_type": "programming",
                "blocks_json": "[{\"type\":\"route\",\"config\":{\"label\":\"Programming Task\",\"condition\":\"task.type == 'programming'\"}},{\"type\":\"executor\",\"config\":{\"model\":\"minimax/minimax-m2.5\",\"executor\":\"OpenRouter\",\"label\":\"Minimax \u2014 First Attempt\"}},{\"type\":\"review\",\"config\":{\"model\":\"anthropic/claude-opus-4.6\",\"executor\":\"Claude CLI\",\"label\":\"Opus Reviews Output\",\"pass_action\":\"skip_to_done\"}},{\"type\":\"retry\",\"config\":{\"model\":\"minimax/minimax-m2.5\",\"executor\":\"OpenRouter\",\"label\":\"Minimax \u2014 Retry w/ Notes\",\"max_retries\":1,\"include_review_notes\":true}},{\"type\":\"review\",\"config\":{\"model\":\"anthropic/claude-opus-4.6\",\"executor\":\"Claude CLI\",\"label\":\"Opus Reviews Again\",\"pass_action\":\"skip_to_done\"}},{\"type\":\"escalate\",\"config\":{\"model\":\"claude-cli\",\"executor\":\"Claude CLI\",\"label\":\"Claude Takes Over\",\"on_limit\":\"queue\"}},{\"type\":\"done\",\"config\":{\"label\":\"Task Complete\"}}]",
                "is_active": 1,
                "created_at": "2026-02-23T22:31:24+00:00",
                "updated_at": "2026-02-24T12:47:01+00:00"
        },
        {
                "id": 2,
                "name": "Reviewing Pipeline",
                "description": "",
                "task_type": "review",
                "blocks_json": "[{\"type\":\"route\",\"config\":{\"label\":\"Review Task\",\"condition\":\"task.type == 'review'\"}},{\"type\":\"executor\",\"config\":{\"model\":\"anthropic/claude-opus-4.6\",\"executor\":\"Claude CLI\",\"label\":\"Claude\",\"on_limit\":\"queue\"}},{\"type\":\"done\",\"config\":{\"label\":\"Task Complete\"}}]",
                "is_active": 1,
                "created_at": "2026-02-24T12:44:17+00:00",
                "updated_at": "2026-02-24T12:49:11+00:00"
        },
        {
                "id": 3,
                "name": "Reporting Pipeline",
                "description": "",
                "task_type": "reporting",
                "blocks_json": "[{\"type\":\"route\",\"config\":{\"label\":\"Reporting Task\",\"condition\":\"task.type == 'reporting'\"}},{\"type\":\"executor\",\"config\":{\"model\":\"anthropic/claude-opus-4.6\",\"executor\":\"Claude CLI\",\"label\":\"Claude\",\"on_limit\":\"queue\"}},{\"type\":\"done\",\"config\":{\"label\":\"Task Complete\"}}]",
                "is_active": 1,
                "created_at": "2026-02-24T12:51:03.906275",
                "updated_at": "2026-02-24T12:53:33+00:00"
        },
        {
                "id": 4,
                "name": "Architecture Pipeline",
                "description": "",
                "task_type": "architecture",
                "blocks_json": "[{\"type\":\"route\",\"config\":{\"label\":\"Architecture Task\",\"condition\":\"task.type == 'architecture'\"}},{\"type\":\"executor\",\"config\":{\"model\":\"anthropic/claude-opus-4.6\",\"executor\":\"Claude CLI\",\"label\":\"Claude\",\"on_limit\":\"queue\"}},{\"type\":\"done\",\"config\":{\"label\":\"Task Complete\"}}]",
                "is_active": 1,
                "created_at": "2026-02-24T12:51:03.906406",
                "updated_at": "2026-02-24T12:53:07+00:00"
        }
]

    for p in pipelines_data:
        cur.execute("""
            INSERT INTO pipelines (id, name, description, task_type, blocks_json, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p['id'], p['name'], p['description'], p['task_type'], 
            p['blocks_json'], p['is_active'], p['created_at'], p['updated_at']
        ))

    con.commit()
    con.close()
    print(f"Seeded {len(pipelines_data)} pipelines")


def seed_agents():
    """Seed agents table with all agent configurations."""
    con = connect()
    cur = con.cursor()

    # Clear existing agents
    cur.execute("DELETE FROM agents")

    agents_data = [
        {
                "id": 1,
                "name": "Programmer",
                "role": "programming",
                "model": "openai/gpt-5.2",
                "pipeline_id": 1,
                "is_active": 1,
                "created_at": "2026-02-23T22:31:24+00:00",
                "updated_at": "2026-02-24T12:54:36+00:00"
        },
        {
                "id": 2,
                "name": "Architect",
                "role": "architecture",
                "model": "openai/gpt-5.2",
                "pipeline_id": 4,
                "is_active": 1,
                "created_at": "2026-02-23T22:31:24+00:00",
                "updated_at": "2026-02-24T12:56:28+00:00"
        },
        {
                "id": 3,
                "name": "Reviewer",
                "role": "reviewing",
                "model": "openai/gpt-5.2",
                "pipeline_id": 2,
                "is_active": 1,
                "created_at": "2026-02-23T22:31:24+00:00",
                "updated_at": "2026-02-24T12:56:34+00:00"
        },
        {
                "id": 4,
                "name": "Reporter",
                "role": "reporting",
                "model": "openai/gpt-5.2",
                "pipeline_id": 3,
                "is_active": 1,
                "created_at": "2026-02-23T22:31:24+00:00",
                "updated_at": "2026-02-24T12:56:39+00:00"
        }
]

    for a in agents_data:
        cur.execute("""
            INSERT INTO agents (id, name, role, model, pipeline_id, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            a['id'], a['name'], a['role'], a['model'],
            a['pipeline_id'], a['is_active'], a['created_at'], a['updated_at']
        ))

    con.commit()
    con.close()
    print(f"Seeded {len(agents_data)} agents")


def seed_agent_files():
    """Seed agent files (SOUL.md, INSTRUCTIONS.md, CONTEXT.md)."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agents_dir = os.path.join(base_dir, "data", "agents")

    agent_files_data = {
        "Programmer": {
                "SOUL.md": "# Programmer \u2014 Soul\n\nYou are **Programmer**, a programming agent in the ZeroClaw autonomous task system.\n\n## Personality\n- Professional and focused\n- Clear and concise in communication\n- Thorough in your work\n\n## Values\n- Accuracy over speed\n- Completeness over brevity when it matters\n- Always explain your reasoning\n",
                "INSTRUCTIONS.md": "# Programmer \u2014 Instructions\n\n## Role\nYou are the **programming** agent. Your primary responsibilities:\n\n- Write clean, well-structured code\n- Include full file paths and complete code blocks\n- Handle edge cases and error conditions\n- Follow existing project patterns and conventions\n\n## Output Format\n- Return your output in markdown\n- Include a short \"Result\" section first with a summary\n- Be specific and actionable\n",
                "CONTEXT.md": "# Programmer \u2014 Context\n\n## Project Context\nThis agent operates within the ZeroClaw task management system.\n\n## Conventions\n- Follow existing code patterns and project conventions\n- Use the tech stack already established in the project\n"
        },
        "Architect": {
                "SOUL.md": "# Architect \u2014 Soul\n\nYou are **Architect**, a architecture agent in the ZeroClaw autonomous task system.\n\n## Personality\n- Professional and focused\n- Clear and concise in communication\n- Thorough in your work\n\n## Values\n- Accuracy over speed\n- Completeness over brevity when it matters\n- Always explain your reasoning\n",
                "INSTRUCTIONS.md": "# Architect \u2014 Instructions\n\n## Role\nYou are the **architecture** agent. Your primary responsibilities:\n\n- Make high-level design decisions\n- Identify tradeoffs between approaches\n- Create concrete implementation plans\n- Consider scalability, maintainability, and security\n\n## Output Format\n- Return your output in markdown\n- Include a short \"Result\" section first with a summary\n- Be specific and actionable\n",
                "CONTEXT.md": "# Architect \u2014 Context\n\n## Project Context\nThis agent operates within the ZeroClaw task management system.\n\n## Conventions\n- Follow existing code patterns and project conventions\n- Use the tech stack already established in the project\n"
        },
        "Reviewer": {
                "SOUL.md": "# Reviewer \u2014 Soul\n\nYou are **Reviewer**, a reviewing agent in the ZeroClaw autonomous task system.\n\n## Personality\n- Professional and focused\n- Clear and concise in communication\n- Thorough in your work\n\n## Values\n- Accuracy over speed\n- Completeness over brevity when it matters\n- Always explain your reasoning\n",
                "INSTRUCTIONS.md": "# Reviewer \u2014 Instructions\n\n## Role\nYou are the **reviewing** agent. Your primary responsibilities:\n\n- Thoroughly review code and deliverables\n- Identify bugs, issues, and risks\n- Propose specific fixes and improvements\n- Give a clear PASS or FAIL verdict\n\n## Output Format\n- Return your output in markdown\n- Include a short \"Result\" section first with a summary\n- Be specific and actionable\n",
                "CONTEXT.md": "# Reviewer \u2014 Context\n\n## Project Context\nThis agent operates within the ZeroClaw task management system.\n\n## Conventions\n- Follow existing code patterns and project conventions\n- Use the tech stack already established in the project\n"
        },
        "Reporter": {
                "SOUL.md": "# Reporter \u2014 Soul\n\nYou are **Reporter**, a reporting agent in the ZeroClaw autonomous task system.\n\n## Personality\n- Professional and focused\n- Clear and concise in communication\n- Thorough in your work\n\n## Values\n- Accuracy over speed\n- Completeness over brevity when it matters\n- Always explain your reasoning\n",
                "INSTRUCTIONS.md": "# Reporter \u2014 Instructions\n\n## Role\nYou are the **reporting** agent. Your primary responsibilities:\n\n- Summarize work clearly and concisely\n- Highlight key findings and next steps\n- Use structured formatting for readability\n- Include metrics where available\n\n## Output Format\n- Return your output in markdown\n- Include a short \"Result\" section first with a summary\n- Be specific and actionable\n",
                "CONTEXT.md": "# Reporter \u2014 Context\n\n## Project Context\nThis agent operates within the ZeroClaw task management system.\n\n## Conventions\n- Follow existing code patterns and project conventions\n- Use the tech stack already established in the project\n"
        }
}

    for agent_name, files in agent_files_data.items():
        agent_dir = os.path.join(agents_dir, agent_name)
        os.makedirs(agent_dir, exist_ok=True)
        for filename, content in files.items():
            filepath = os.path.join(agent_dir, filename)
            with open(filepath, "w") as f:
                f.write(content)

    print(f"Seeded agent files for {len(agent_files_data)} agents")


def seed_all():
    """Seed all data - call this to initialize fresh dashboard."""
    print("Seeding dashboard with initial data...")
    seed_pipelines()
    seed_agents()
    seed_agent_files()
    print("Done! Dashboard initialized.")


if __name__ == "__main__":
    seed_all()
