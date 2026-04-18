from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nanobot.command.builtin import (
    cmd_task_create,
    cmd_task_current,
    cmd_task_delete,
    cmd_task_help,
    cmd_task_list,
    cmd_task_mark,
    cmd_task_memory_add,
    cmd_task_memory_list,
    cmd_task_move,
    cmd_task_show,
    cmd_task_status,
    cmd_task_tree,
    cmd_task_update,
    cmd_session_bind,
    cmd_session_unbind,
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


def _make_session_ctx(raw: str, workspace, session: Session | None = None) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=raw)
    loop = NS(workspace=workspace, sessions=NS(save=lambda s: None))
    tail = raw[len("/session"):].strip() if raw.startswith("/session") else raw
    parts = tail.split(None, 1)
    args = parts[1] if len(parts) == 2 else ""
    return CommandContext(msg=msg, session=session, key=msg.session_key, raw=raw, args=args, loop=loop)


@pytest.mark.asyncio
async def test_task_help_returns_usage() -> None:
    ctx = _make_ctx("/task help", Path("/tmp"))
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
async def test_task_update_modifies_title_description_status(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    child = store.create_task(title="Child task", description="Old desc")

    ctx = _make_ctx(
        f'/task update {child.id} --title "Updated child" --description "New description" --status doing',
        tmp_path,
    )
    out = await cmd_task_update(ctx)
    assert "Updated task" in out.content
    assert "title=Updated child" in out.content
    assert "description=New description" in out.content
    assert "status=doing" in out.content

    reloaded = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded is not None
    assert reloaded.title == "Updated child"
    assert reloaded.description == "New description"
    assert reloaded.status == "doing"


@pytest.mark.asyncio
async def test_task_update_does_not_accept_parent_flag(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child")

    ctx = _make_ctx(f"/task update {child.id} --parent {parent.id}", tmp_path)
    out = await cmd_task_update(ctx)
    # --parent is silently ignored; "no field to update" error expected
    assert "Please provide at least one field" in out.content
    reloaded = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded is not None
    assert reloaded.parent_id is None


@pytest.mark.asyncio
async def test_task_command_sequence_flow(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    root_a = store.create_task(title="Root A")
    root_b = store.create_task(title="Root B")
    child = store.create_task(title="Child", description="Initial", parent_id=root_a.id)

    # tree with --depth 1 should show root_a and child
    ctx_tree_depth = _make_ctx(f"/task tree {root_a.id} --depth 1", tmp_path)
    out_tree_depth = await cmd_task_tree(ctx_tree_depth)
    assert child.id in out_tree_depth.content

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
async def test_task_memory_add_and_list(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Memory task")

    ctx_add = _make_ctx(f"/task memory add {task.id} | Remember the deadline is Friday", tmp_path)
    out_add = await cmd_task_memory_add(ctx_add)
    assert "Added task memory" in out_add.content

    ctx_list = _make_ctx(f"/task memory list {task.id}", tmp_path)
    out_list = await cmd_task_memory_list(ctx_list)
    assert "## Task Memory" in out_list.content
    assert "Remember the deadline is Friday" in out_list.content


@pytest.mark.asyncio
async def test_session_bind_and_unbind_updates_session_metadata(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Bind session")
    session = Session(key="cli:direct")
    saved = []

    def save_callback(session_obj):
        saved.append(session_obj)

    loop = NS(workspace=tmp_path, sessions=NS(save=save_callback))
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=f"/session bind {task.id}")
    ctx = CommandContext(msg=msg, session=session, key=session.key, raw=msg.content, args=task.id, loop=loop)

    out = await cmd_session_bind(ctx)
    assert "bound to task" in out.content
    assert session.metadata["task_id"] == task.id
    assert saved and saved[-1] is session

    unbind_msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/session unbind")
    ctx_unbind = CommandContext(msg=unbind_msg, session=session, key=session.key, raw=unbind_msg.content, args="", loop=loop)
    out_unbind = await cmd_session_unbind(ctx_unbind)
    assert "unbound" in out_unbind.content.lower()
    assert "task_id" not in session.metadata


@pytest.mark.asyncio
async def test_task_mark_updates_task(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Set status")

    ctx = _make_ctx(f"/task mark {task.id} doing", tmp_path)
    out = await cmd_task_mark(ctx)
    assert "Marked task" in out.content

    reloaded = TaskTreeStore(tmp_path).get_task(task.id)
    assert reloaded is not None
    assert reloaded.status == "doing"


@pytest.mark.asyncio
async def test_task_status_queries_task(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Query status", status="blocked")

    ctx = _make_ctx(f"/task status {task.id}", tmp_path)
    out = await cmd_task_status(ctx)
    assert "blocked" in out.content
    assert task.id in out.content


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
async def test_task_tree_depth_limits_output(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child", parent_id=parent.id)
    grandchild = store.create_task(title="Grandchild", parent_id=child.id)

    # depth=1 should show parent and child but not grandchild
    ctx = _make_ctx(f"/task tree {parent.id} --depth 1", tmp_path)
    out = await cmd_task_tree(ctx)
    assert parent.id in out.content
    assert child.id in out.content
    assert grandchild.id not in out.content


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
async def test_task_delete_refuses_when_has_children(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    store.create_task(title="Child", parent_id=parent.id)

    ctx = _make_ctx(f"/task delete {parent.id}", tmp_path)
    out = await cmd_task_delete(ctx)
    assert "has" in out.content and "child" in out.content
    # Parent should still exist
    assert TaskTreeStore(tmp_path).get_task(parent.id) is not None


@pytest.mark.asyncio
async def test_task_delete_orphan_flag(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child", parent_id=parent.id)

    ctx = _make_ctx(f"/task delete {parent.id} --orphan", tmp_path)
    out = await cmd_task_delete(ctx)
    assert "Deleted task" in out.content

    reloaded_child = TaskTreeStore(tmp_path).get_task(child.id)
    assert reloaded_child is not None
    assert reloaded_child.parent_id is None


@pytest.mark.asyncio
async def test_task_delete_cascade_flag(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent")
    child = store.create_task(title="Child", parent_id=parent.id)
    grandchild = store.create_task(title="Grandchild", parent_id=child.id)

    ctx = _make_ctx(f"/task delete {parent.id} --cascade", tmp_path)
    out = await cmd_task_delete(ctx)
    assert "descendants" in out.content

    s = TaskTreeStore(tmp_path)
    assert s.get_task(parent.id) is None
    assert s.get_task(child.id) is None
    assert s.get_task(grandchild.id) is None


@pytest.mark.asyncio
async def test_task_current_shows_bound_task(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="My bound task")
    session = Session(key="cli:direct")
    session.metadata["task_id"] = task.id

    loop = NS(workspace=tmp_path, sessions=NS(save=lambda s: None))
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/task")
    ctx = CommandContext(msg=msg, session=session, key=session.key, raw=msg.content, args="", loop=loop)

    out = await cmd_task_current(ctx)
    assert "My bound task" in out.content


@pytest.mark.asyncio
async def test_task_current_shows_help_when_no_task_bound(tmp_path: Path) -> None:
    session = Session(key="cli:direct")
    loop = NS(workspace=tmp_path, sessions=NS(save=lambda s: None))
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/task")
    ctx = CommandContext(msg=msg, session=session, key=session.key, raw=msg.content, args="", loop=loop)

    out = await cmd_task_current(ctx)
    assert "nanobot task commands" in out.content


@pytest.mark.asyncio
async def test_bidirectional_bind_unbind(tmp_path: Path) -> None:
    """Binding a session updates both session.metadata and task.bound_sessions."""
    from nanobot.task.store import TaskTreeStore as TStore
    store = TStore(tmp_path)
    task = store.create_task(title="Bidir task")
    session = Session(key="cli:test_bidir")
    saved: list[Session] = []

    loop = NS(workspace=tmp_path, sessions=NS(
        save=lambda s: saved.append(s),
        get_or_create=lambda key: Session(key=key),
    ))
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="test_bidir", content=f"/session bind {task.id}")
    ctx = CommandContext(msg=msg, session=session, key=session.key, raw=msg.content, args=task.id, loop=loop)

    await cmd_session_bind(ctx)
    assert session.metadata.get("task_id") == task.id
    assert session.key in TStore(tmp_path).get_bound_sessions(task.id)

    # Unbind
    unbind_msg = InboundMessage(channel="cli", sender_id="u1", chat_id="test_bidir", content="/session unbind")
    ctx_u = CommandContext(msg=unbind_msg, session=session, key=session.key, raw=unbind_msg.content, args="", loop=loop)
    await cmd_session_unbind(ctx_u)
    assert "task_id" not in session.metadata
    assert session.key not in TStore(tmp_path).get_bound_sessions(task.id)


@pytest.mark.asyncio
async def test_task_delete_unbinds_sessions(tmp_path: Path) -> None:
    """Deleting a task automatically clears task_id from all bound sessions."""
    from nanobot.task.store import TaskTreeStore as TStore
    store = TStore(tmp_path)
    task = store.create_task(title="Task to delete")
    store.bind_session(task.id, "cli:sess1")

    sessions_store: dict[str, Session] = {}

    def _get_or_create(key: str) -> Session:
        if key not in sessions_store:
            s = Session(key=key)
            s.metadata["task_id"] = task.id
            sessions_store[key] = s
        return sessions_store[key]

    def _save(s: Session) -> None:
        sessions_store[s.key] = s

    loop = NS(workspace=tmp_path, sessions=NS(get_or_create=_get_or_create, save=_save))
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=f"/task delete {task.id}")
    ctx = CommandContext(msg=msg, session=None, key="cli:direct", raw=msg.content, args=task.id, loop=loop)

    out = await cmd_task_delete(ctx)
    assert "Deleted" in out.content
    # Session's task_id should be cleared
    sess = sessions_store.get("cli:sess1")
    assert sess is not None
    assert "task_id" not in sess.metadata
