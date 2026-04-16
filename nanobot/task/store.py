from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import ensure_dir


TASK_INDEX_FILENAME = "index.json"


@dataclass
class TaskNode:
    id: str
    title: str
    description: str = ""
    status: str = "todo"
    task_type: str = "normal"
    parent_id: str | None = None
    owner: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskNode":
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            status=data.get("status", "todo"),
            task_type=data.get("task_type", "normal"),
            parent_id=data.get("parent_id"),
            owner=data.get("owner"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            metadata=data.get("metadata", {}),
        )


class TaskTreeStore:
    """Persistent task tree store for workspace-local tasks."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.tasks_dir = ensure_dir(workspace / "tasks")
        self.tasks_file = self.tasks_dir / TASK_INDEX_FILENAME
        self._ensure_index()

    def _ensure_index(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        if not self.tasks_file.exists():
            self.tasks_file.write_text(json.dumps({"tasks": []}, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load(self) -> dict[str, TaskNode]:
        if not self.tasks_file.exists():
            self._ensure_index()
        try:
            raw = json.loads(self.tasks_file.read_text(encoding="utf-8"))
        except Exception:
            raw = {"tasks": []}
        return {
            task_data["id"]: TaskNode.from_dict(task_data)
            for task_data in raw.get("tasks", [])
            if isinstance(task_data, dict) and "id" in task_data
        }

    def _save(self, tasks: dict[str, TaskNode]) -> None:
        self.tasks_file.write_text(
            json.dumps({"tasks": [task.to_dict() for task in tasks.values()]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def list_tasks(self, status: str | None = None) -> list[TaskNode]:
        tasks = list(self._load().values())
        if status:
            status_lower = status.lower()
            return [task for task in tasks if task.status.lower() == status_lower]
        return sorted(tasks, key=lambda t: t.created_at)

    def get_task(self, task_id: str) -> TaskNode | None:
        return self._load().get(task_id)

    def get_parent(self, task_id: str) -> TaskNode | None:
        task = self.get_task(task_id)
        if not task or not task.parent_id:
            return None
        return self.get_task(task.parent_id)

    def get_children(self, parent_id: str) -> list[TaskNode]:
        return [task for task in self._load().values() if task.parent_id == parent_id]

    def get_root_tasks(self) -> list[TaskNode]:
        return [task for task in self._load().values() if not task.parent_id]

    def _get_task_dir(self, task_id: str) -> Path:
        task_dir = self.tasks_dir / task_id
        return ensure_dir(task_dir)

    def _get_task_memory_file(self, task_id: str) -> Path:
        return self._get_task_dir(task_id) / "memory.jsonl"

    def _normalize_task_memory_entry(self, entry: dict[str, Any]) -> dict[str, str]:
        return {
            "id": entry.get("id") or uuid.uuid4().hex[:8],
            "timestamp": entry.get("timestamp", datetime.now().isoformat()),
            "content": entry.get("content", ""),
        }

    def add_task_memory(self, task_id: str, content: str) -> bool:
        if not self.get_task(task_id):
            return False
        entry = self._normalize_task_memory_entry({
            "content": content,
        })
        path = self._get_task_memory_file(task_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True

    def read_task_memory(self, task_id: str, max_entries: int | None = None) -> list[dict[str, str]]:
        path = self._get_task_memory_file(task_id)
        if not path.exists():
            return []
        entries: list[dict[str, str]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                entries.append(self._normalize_task_memory_entry(entry))
        if max_entries is not None:
            return entries[-max_entries:]
        return entries

    def update_task_memory(self, task_id: str, entry_id: str, content: str) -> bool:
        path = self._get_task_memory_file(task_id)
        if not self.get_task(task_id) or not path.exists():
            return False
        entries = self.read_task_memory(task_id)
        updated = False
        for entry in entries:
            if entry["id"] == entry_id:
                entry["content"] = content
                entry["timestamp"] = datetime.now().isoformat()
                updated = True
                break
        if not updated:
            return False
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True

    def delete_task_memory(self, task_id: str, entry_id: str) -> bool:
        path = self._get_task_memory_file(task_id)
        if not self.get_task(task_id) or not path.exists():
            return False
        entries = self.read_task_memory(task_id)
        kept = [entry for entry in entries if entry["id"] != entry_id]
        if len(kept) == len(entries):
            return False
        with open(path, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True

    def build_task_memory_context(self, task_id: str, max_entries: int = 20) -> str:
        entries = self.read_task_memory(task_id, max_entries=max_entries)
        if not entries:
            return ""
        lines = ["## Task Memory"]
        for entry in entries:
            lines.append(f"- [{entry['timestamp']}] ({entry['id']}) {entry['content']}")
        return "\n".join(lines)

    def is_descendant(self, task_id: str, ancestor_id: str) -> bool:
        current = self.get_task(task_id)
        while current and current.parent_id:
            if current.parent_id == ancestor_id:
                return True
            current = self.get_parent(current.id)
        return False

    def build_task_tree(self, task_id: str | None = None) -> str | None:
        def _render(node: TaskNode, prefix: str = "") -> list[str]:
            lines = [f"{prefix}- `{node.id}` {node.title} [{node.status}] ({node.task_type})"]
            children = sorted(self.get_children(node.id), key=lambda t: t.title)
            for child in children:
                lines.extend(_render(child, prefix + "  "))
            return lines

        if task_id:
            root = self.get_task(task_id)
            if not root:
                return None
            return "\n".join(_render(root))

        roots = sorted(self.get_root_tasks(), key=lambda t: t.title)
        if not roots:
            return "No tasks found."
        lines: list[str] = []
        for root in roots:
            lines.extend(_render(root))
        return "\n".join(lines)

    def create_task(
        self,
        title: str,
        description: str = "",
        task_type: str = "normal",
        parent_id: str | None = None,
        owner: str | None = None,
        status: str = "todo",
    ) -> TaskNode:
        tasks = self._load()
        task_id = uuid.uuid4().hex[:8]
        while task_id in tasks:
            task_id = uuid.uuid4().hex[:8]
        task = TaskNode(
            id=task_id,
            title=title.strip(),
            description=description.strip(),
            status=status.strip() or "todo",
            task_type=task_type.strip() or "normal",
            parent_id=parent_id,
            owner=owner,
        )
        tasks[task.id] = task
        self._save(tasks)
        return task

    def update_task(self, task_id: str, **updates: Any) -> TaskNode | None:
        tasks = self._load()
        task = tasks.get(task_id)
        if not task:
            return None
        for key, value in updates.items():
            if value is None:
                continue
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = datetime.now().isoformat()
        tasks[task_id] = task
        self._save(tasks)
        return task

    def delete_task(self, task_id: str) -> bool:
        tasks = self._load()
        if task_id not in tasks:
            return False
        tasks.pop(task_id)
        # Clear any child references so tree remains valid.
        for child in tasks.values():
            if child.parent_id == task_id:
                child.parent_id = None
        self._save(tasks)
        # Remove the task's memory directory if it exists.
        task_dir = self.tasks_dir / task_id
        if task_dir.exists():
            import shutil
            shutil.rmtree(task_dir)
        return True

    def task_path(self, task_id: str) -> list[TaskNode]:
        path: list[TaskNode] = []
        tasks = self._load()
        current = tasks.get(task_id)
        while current:
            path.insert(0, current)
            current = tasks.get(current.parent_id) if current.parent_id else None
        return path

    def build_task_summary(self, task_id: str) -> str | None:
        task = self.get_task(task_id)
        if not task:
            return None
        path = self.task_path(task_id)
        path_str = " > ".join([node.title for node in path])
        lines = [
            f"Task ID: {task.id}",
            f"Title: {task.title}",
            f"Status: {task.status}",
            f"Type: {task.task_type}",
        ]
        if task.owner:
            lines.append(f"Owner: {task.owner}")
        if path_str:
            lines.append(f"Path: {path_str}")
        children = self.get_children(task.id)
        if children:
            lines.append(
                "Children: " + ", ".join(f"{child.title} ({child.id})" for child in children)
            )
        if task.description:
            lines.append(f"Description: {task.description}")
        return "\n".join(lines)
