from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.tools.task_memory import (
    TaskMemoryAddTool,
    TaskMemoryDeleteTool,
    TaskMemoryListTool,
    TaskMemoryUpdateTool,
)
from nanobot.task.store import TaskTreeStore


@pytest.mark.asyncio
async def test_task_memory_add_tool_adds_entry(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Test task")

    add_tool = TaskMemoryAddTool(tmp_path)
    add_tool.set_context(task.id)
    result = await add_tool.execute(content="Remember the deadline")

    assert "Added task memory" in result
    entries = store.read_task_memory(task.id)
    assert len(entries) == 1
    assert entries[0]["content"] == "Remember the deadline"


@pytest.mark.asyncio
async def test_task_memory_list_tool_returns_entries(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Test task")
    store.add_task_memory(task.id, "First fact")
    store.add_task_memory(task.id, "Second fact")

    list_tool = TaskMemoryListTool(tmp_path)
    list_tool.set_context(task.id)
    result = await list_tool.execute(max_entries=10)

    assert "Task `" in result
    assert "First fact" in result
    assert "Second fact" in result


@pytest.mark.asyncio
async def test_task_memory_update_tool_updates_entry(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Test task")
    store.add_task_memory(task.id, "Old content")
    entry_id = store.read_task_memory(task.id)[0]["id"]

    update_tool = TaskMemoryUpdateTool(tmp_path)
    update_tool.set_context(task.id)
    result = await update_tool.execute(entry_id=entry_id, content="New content")

    assert "Updated task memory entry" in result
    entries = store.read_task_memory(task.id)
    assert entries[0]["content"] == "New content"
    assert entries[0]["id"] == entry_id


@pytest.mark.asyncio
async def test_task_memory_delete_tool_deletes_entry(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Test task")
    store.add_task_memory(task.id, "Delete me")
    entry_id = store.read_task_memory(task.id)[0]["id"]

    delete_tool = TaskMemoryDeleteTool(tmp_path)
    delete_tool.set_context(task.id)
    result = await delete_tool.execute(entry_id=entry_id)

    assert "Deleted task memory entry" in result
    assert store.read_task_memory(task.id) == []


@pytest.mark.asyncio
async def test_set_tool_context_with_spawn_and_task_memory_tools(tmp_path: Path) -> None:
    """Regression test: _set_tool_context must not pass task_id to SpawnTool."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager") as mock_session_mgr, \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr, \
         patch("nanobot.agent.loop.Consolidator"), \
         patch("nanobot.agent.loop.Dream"):
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        mock_session_mgr.return_value.get_or_create.return_value = MagicMock()
        loop = AgentLoop(
            bus=bus, provider=provider, workspace=tmp_path,
        )
        # task_id is now passed as an explicit parameter, not via a shared instance field.
        # Should not raise: SpawnTool expects (channel, chat_id), task memory tools expect (task_id).
        loop._set_tool_context("cli", "test", "msg-1", task_id="task-123")
        # Verify task memory tool received the bound task_id.
        add_tool = loop.tools.get("task_memory_add")
        assert add_tool is not None
        assert add_tool._task_id == "task-123"
