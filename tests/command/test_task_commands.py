from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nanobot.command.builtin import (
    cmd_task_attach,
    cmd_task_children,
    cmd_task_create,
    cmd_task_delete,
    cmd_task_detach,
    cmd_task_help,
    cmd_task_list,
    cmd_task_move,
    cmd_task_memory_add,
    cmd_task_memory_view,
    cmd_task_show,
    cmd_task_status,
    cmd_task_tree,
    cmd_task_update,
)
from nanobot.command.router import CommandContext
from nanobot.task.store import TaskTreeStore
from nanobot.session.manager import Session
from nanobot.bus.events import InboundMessage


def _make_ctx(raw: str, workspace, session: Session | None = None) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=raw)
    loop = NS(workspace=workspace, sessions=NS(save=lambda session: None))
    tail = raw[len("/task"):].strip() if raw.startswith("/task") else raw
    parts = tail.split(None, 1)
    args = parts[1] if len(parts) == 2 else ""
    return CommandContext(msg=msg, session=session, key=msg.session_key, raw=raw, args=args, loop=loop)


@pytest.mark.asyncio
async def test_task_help_returns_usage() -> None:
    ctx = _make_ctx("/task", Path("/tmp"))
    out = await cmd_task_help(ctx)
    assert "nanobot task commands" in out.content
    assert "/task create" in out.content


@pytest.mark.asyncio
async def test_task_create_creates_task(tmp_path: Path) -> None:
    ctx = _make_ctx("/task create Write tests | Cover task commands", tmp_path)
    out = await cmd_task_create(ctx)
    assert "Created task" in out.content
    assert "Write tests" in out.content

    store = TaskTreeStore(tmp_path)
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Write tests"


@pytest.mark.asyncio
async def test_task_create_with_parent(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent task")

    ctx = _make_ctx(
        f"/task create --parent {parent.id} Child task | Child description",
        tmp_path,
    )
    out = await cmd_task_create(ctx)
    assert "Created task" in out.content
    assert "Child task" in out.content

    child_tasks = [
        task for task in TaskTreeStore(tmp_path).list_tasks() if task.title == "Child task"
    ]
    assert len(child_tasks) == 1
    child = child_tasks[0]
    assert child.parent_id == parent.id


@pytest.mark.asyncio
async def test_task_update_modifies_title_description_status_parent(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent task")
    child = store.create_task(title="Child task", description="Old desc")

    ctx = _make_ctx(
        f"/task update {child.id} --title \"Updated child\" --description \"New description\" --status doing --parent {parent.id}",
        tmp_path,
    )
    out = await cmd_task_update(ctx)
    assert "Updated task" in out.content
    assert "title=Updated child" in out.content
    assert "description=New description" in out.content
    assert "status=doing" in out.content
    assert f"parent_id={parent.id}" in out.content

    reloaded = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded is not None
    assert reloaded.title == "Updated child"
    assert reloaded.description == "New description"
    assert reloaded.status == "doing"
    assert reloaded.parent_id == parent.id


@pytest.mark.asyncio
async def test_task_command_sequence_flow(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    root_a = store.create_task(title="Root A")
    root_b = store.create_task(title="Root B")
    child = store.create_task(title="Child", description="Initial")

    ctx_create = _make_ctx(f"/task update {child.id} --parent {root_a.id} --status doing", tmp_path)
    out_create = await cmd_task_update(ctx_create)
    assert "Updated task" in out_create.content

    ctx_children = _make_ctx(f"/task children {root_a.id}", tmp_path)
    out_children = await cmd_task_children(ctx_children)
    assert child.id in out_children.content
    assert "Child" in out_children.content

    ctx_move = _make_ctx(f"/task move {child.id} {root_b.id}", tmp_path)
    out_move = await cmd_task_move(ctx_move)
    assert "Moved task" in out_move.content

    ctx_tree = _make_ctx(f"/task tree {root_b.id}", tmp_path)
    out_tree = await cmd_task_tree(ctx_tree)
    assert root_b.id in out_tree.content
    assert child.id in out_tree.content

    ctx_show = _make_ctx(f"/task show {child.id}", tmp_path)
    out_show = await cmd_task_show(ctx_show)
    assert "Child" in out_show.content
    assert "doing" in out_show.content

    reloaded_child = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded_child is not None
    assert reloaded_child.parent_id == root_b.id


@pytest.mark.asyncio
async def test_task_list_filters_and_formats(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task1 = store.create_task(title="One", status="todo")
    task2 = store.create_task(title="Two", status="done")

    ctx = _make_ctx("/task list done", tmp_path)
    out = await cmd_task_list(ctx)
    assert task2.id in out.content
    assert task1.id not in out.content

    ctx_all = _make_ctx("/task list", tmp_path)
    out_all = await cmd_task_list(ctx_all)
    assert task1.id in out_all.content
    assert task2.id in out_all.content


@pytest.mark.asyncio
async def test_task_show_displays_details(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Inspect feature")

    ctx = _make_ctx(f"/task show {task.id}", tmp_path)
    out = await cmd_task_show(ctx)
    assert task.id in out.content
    assert "Inspect feature" in out.content


@pytest.mark.asyncio
async def test_task_memory_add_and_view(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Memory task")

    ctx_add = _make_ctx(f"/task memory add {task.id} | Remember the deadline is Friday", tmp_path)
    out_add = await cmd_task_memory_add(ctx_add)
    assert "Added task memory" in out_add.content

    ctx_view = _make_ctx(f"/task memory view {task.id}", tmp_path)
    out_view = await cmd_task_memory_view(ctx_view)
    assert "## Task Memory" in out_view.content
    assert "Remember the deadline is Friday" in out_view.content


@pytest.mark.asyncio
async def test_task_attach_and_detach_updates_session_metadata(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Attach session")
    session = Session(key="cli:direct")
    saved = []

    def save_callback(session_obj):
        saved.append(session_obj)

    loop = NS(workspace=tmp_path, sessions=NS(save=save_callback))
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=f"/task attach {task.id}")
    ctx = CommandContext(msg=msg, session=session, key=session.key, raw=msg.content, args=task.id, loop=loop)

    out = await cmd_task_attach(ctx)
    assert "Attached current session to task" in out.content
    assert session.metadata["task_id"] == task.id
    assert saved and saved[-1] is session

    detach_msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/task detach")
    ctx_detach = CommandContext(msg=detach_msg, session=session, key=session.key, raw=detach_msg.content, args="", loop=loop)
    out_detach = await cmd_task_detach(ctx_detach)
    assert "Detached the current task" in out_detach.content
    assert "task_id" not in session.metadata


@pytest.mark.asyncio
async def test_task_status_updates_task(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Set status")

    ctx = _make_ctx(f"/task status {task.id} doing", tmp_path)
    out = await cmd_task_status(ctx)
    assert "Updated task" in out.content

    reloaded = TaskTreeStore(tmp_path).get_task(task.id)
    assert reloaded is not None
    assert reloaded.status == "doing"


@pytest.mark.asyncio
async def test_task_children_lists_direct_children(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child", parent_id=parent.id)

    ctx = _make_ctx(f"/task children {parent.id}", tmp_path)
    out = await cmd_task_children(ctx)
    assert "Children of" in out.content
    assert child.id in out.content


@pytest.mark.asyncio
async def test_task_tree_shows_subtree(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child", parent_id=parent.id)

    ctx = _make_ctx(f"/task tree {parent.id}", tmp_path)
    out = await cmd_task_tree(ctx)
    assert parent.id in out.content
    assert child.id in out.content


@pytest.mark.asyncio
async def test_task_move_changes_parent(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child")

    ctx = _make_ctx(f"/task move {child.id} {parent.id}", tmp_path)
    out = await cmd_task_move(ctx)
    assert "Moved task" in out.content

    reloaded = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded is not None
    assert reloaded.parent_id == parent.id


@pytest.mark.asyncio
async def test_task_delete_orphans_children(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child", parent_id=parent.id)

    ctx = _make_ctx(f"/task delete {parent.id}", tmp_path)
    out = await cmd_task_delete(ctx)
    assert "Deleted task" in out.content

    reloaded_child = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded_child is not None
    assert reloaded_child.parent_id is None
