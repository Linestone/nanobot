from pathlib import Path

from nanobot.task.store import TaskTreeStore


def test_task_tree_store_creates_and_loads_tasks(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Prepare report", description="Collect metrics and write summary")

    assert task.id
    assert task.title == "Prepare report"
    assert task.description == "Collect metrics and write summary"
    assert task.status == "todo"
    assert store.get_task(task.id) is not None

    loaded = store.list_tasks()
    assert len(loaded) == 1
    assert loaded[0].id == task.id

    summary = store.build_task_summary(task.id)
    assert "Task ID: " in summary
    assert "Prepare report" in summary


def test_task_tree_store_updates_status_and_parent(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Project launch")
    child = store.create_task(title="Write docs", parent_id=parent.id)

    updated = store.update_task(child.id, status="doing", owner="alice")
    assert updated is not None
    assert updated.status == "doing"
    assert updated.owner == "alice"

    path = store.task_path(child.id)
    assert [node.id for node in path] == [parent.id, child.id]


def test_task_tree_store_parent_child_relationships(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent task")
    child1 = store.create_task(title="Child 1", parent_id=parent.id)
    child2 = store.create_task(title="Child 2", parent_id=parent.id)

    assert child1.parent_id == parent.id
    assert child2.parent_id == parent.id
    assert store.get_parent(child1.id) is not None
    assert store.get_parent(child1.id).id == parent.id

    children = store.get_children(parent.id)
    assert {child.id for child in children} == {child1.id, child2.id}

    summary = store.build_task_summary(parent.id)
    assert "Children:" in summary
    assert child1.id in summary and child2.id in summary


def test_task_tree_store_delete_clears_parent_refs(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    parent = store.create_task(title="Parent task")
    child = store.create_task(title="Child task", parent_id=parent.id)

    assert store.delete_task(parent.id)
    reloaded_child = store.get_task(child.id)
    assert reloaded_child is not None
    assert reloaded_child.parent_id is None


def test_task_tree_store_task_memory_context(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Memory task")

    assert store.add_task_memory(task.id, "First note")
    assert store.add_task_memory(task.id, "Second note")

    entries = store.read_task_memory(task.id)
    assert len(entries) == 2
    assert entries[0]["content"] == "First note"
    assert entries[1]["content"] == "Second note"
    assert "id" in entries[0] and "id" in entries[1]

    context = store.build_task_memory_context(task.id)
    assert "## Task Memory" in context
    assert "First note" in context
    assert "Second note" in context


def test_task_tree_store_updates_and_deletes_task_memory(tmp_path: Path) -> None:
    store = TaskTreeStore(tmp_path)
    task = store.create_task(title="Memory task")

    assert store.add_task_memory(task.id, "First note")
    entries = store.read_task_memory(task.id)
    entry_id = entries[0]["id"]

    assert store.update_task_memory(task.id, entry_id, "Updated note")
    updated = store.read_task_memory(task.id)[0]
    assert updated["content"] == "Updated note"
    assert updated["id"] == entry_id

    assert store.delete_task_memory(task.id, entry_id)
    assert store.read_task_memory(task.id) == []
