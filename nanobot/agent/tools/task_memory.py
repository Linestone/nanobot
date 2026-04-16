from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.task.store import TaskTreeStore


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("Optional task ID. If omitted, uses the current session task."),
        content=StringSchema("The memory content to add to the task", min_length=1),
        required=["content"],
    )
)
class TaskMemoryAddTool(Tool):
    """Add a new task memory entry for the current task."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._task_id: str | None = None

    def set_context(self, task_id: str | None) -> None:
        self._task_id = task_id

    @property
    def name(self) -> str:
        return "task_memory_add"

    @property
    def description(self) -> str:
        return (
            "Add a new memory entry to the specified task. "
            "If task_id is omitted, uses the current session-bound task."
        )

    async def execute(self, task_id: str | None = None, content: str = "") -> str:
        task_id = task_id or self._task_id
        if not task_id:
            return "Error: No task_id provided and no active task is bound to this session."
        store = TaskTreeStore(self.workspace)
        if not store.add_task_memory(task_id, content):
            return f"Error: Task `{task_id}` not found or memory could not be added."
        return f"Added task memory to `{task_id}`."


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("Optional task ID. If omitted, uses the current session task."),
        entry_id=StringSchema("The ID of the memory entry to update", min_length=1),
        content=StringSchema("The new content for the memory entry", min_length=1),
        required=["entry_id", "content"],
    )
)
class TaskMemoryUpdateTool(Tool):
    """Update an existing task memory entry."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._task_id: str | None = None

    def set_context(self, task_id: str | None) -> None:
        self._task_id = task_id

    @property
    def name(self) -> str:
        return "task_memory_update"

    @property
    def description(self) -> str:
        return (
            "Update the content of an existing task memory entry. "
            "If task_id is omitted, uses the current session-bound task."
        )

    async def execute(self, entry_id: str, content: str, task_id: str | None = None) -> str:
        task_id = task_id or self._task_id
        if not task_id:
            return "Error: No task_id provided and no active task is bound to this session."
        store = TaskTreeStore(self.workspace)
        if not store.update_task_memory(task_id, entry_id, content):
            return f"Error: Task memory entry `{entry_id}` could not be updated for task `{task_id}`."
        return f"Updated task memory entry `{entry_id}` for task `{task_id}`."


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("Optional task ID. If omitted, uses the current session task."),
        entry_id=StringSchema("The ID of the memory entry to delete", min_length=1),
        required=["entry_id"],
    )
)
class TaskMemoryDeleteTool(Tool):
    """Delete a task memory entry."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._task_id: str | None = None

    def set_context(self, task_id: str | None) -> None:
        self._task_id = task_id

    @property
    def name(self) -> str:
        return "task_memory_delete"

    @property
    def description(self) -> str:
        return (
            "Delete a task memory entry by its ID. "
            "If task_id is omitted, uses the current session-bound task."
        )

    async def execute(self, entry_id: str, task_id: str | None = None) -> str:
        task_id = task_id or self._task_id
        if not task_id:
            return "Error: No task_id provided and no active task is bound to this session."
        store = TaskTreeStore(self.workspace)
        if not store.delete_task_memory(task_id, entry_id):
            return f"Error: Task memory entry `{entry_id}` could not be deleted for task `{task_id}`."
        return f"Deleted task memory entry `{entry_id}` from task `{task_id}`."


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("Optional task ID. If omitted, uses the current session task."),
        max_entries=IntegerSchema(
            20,
            description="Maximum number of task memory entries to return.",
            minimum=1,
        ),
    )
)
class TaskMemoryListTool(Tool):
    """List task memory entries for the current task."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._task_id: str | None = None

    def set_context(self, task_id: str | None) -> None:
        self._task_id = task_id

    @property
    def name(self) -> str:
        return "task_memory_list"

    @property
    def description(self) -> str:
        return (
            "List task memory entries for the specified task. "
            "If task_id is omitted, uses the current session-bound task."
        )

    async def execute(self, task_id: str | None = None, max_entries: int = 20) -> str:
        task_id = task_id or self._task_id
        if not task_id:
            return "Error: No task_id provided and no active task is bound to this session."
        store = TaskTreeStore(self.workspace)
        if not store.get_task(task_id):
            return f"Error: Task `{task_id}` not found."
        entries = store.read_task_memory(task_id, max_entries=max_entries)
        if not entries:
            return f"Task `{task_id}` has no task memory entries."
        lines = [f"Task `{task_id}` memory entries:"]
        for entry in entries:
            lines.append(f"- {entry['id']} [{entry['timestamp']}]: {entry['content']}")
        return "\n".join(lines)
