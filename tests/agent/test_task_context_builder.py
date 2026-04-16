from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.task.store import TaskTreeStore


def test_context_builder_includes_task_memory(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Context task")
    store.add_task_memory(task.id, "Remember this important detail.")

    context = ContextBuilder(tmp_path).build_system_prompt(task_id=task.id)
    assert "# Current Task" in context
    assert "# Current Task Memory" in context
    assert "Remember this important detail." in context
