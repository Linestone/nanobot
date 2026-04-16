"""Tests for Dream task-memory routing — [TASK:<id>] lines split to task_store."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.memory import Dream, MemoryStore
from nanobot.agent.runner import AgentRunResult


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path)
    s.write_soul("# Soul\n- Helpful")
    s.write_user("# User\n- Developer")
    s.write_memory("# Memory\n- Project X active")
    return s


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def mock_runner():
    r = MagicMock()
    r.run = AsyncMock(return_value=AgentRunResult(
        final_content="done",
        stop_reason="completed",
        messages=[],
        tools_used=[],
        usage={},
        tool_events=[],
    ))
    return r


@pytest.fixture
def mock_task_store():
    ts = MagicMock()
    ts.add_task_memory.return_value = True
    ts.build_task_summary.return_value = None
    ts.build_task_memory_context.return_value = ""
    return ts


@pytest.fixture
def dream(store, mock_provider, mock_runner, mock_task_store):
    d = Dream(store=store, provider=mock_provider, model="test-model",
              max_batch_size=5, task_store=mock_task_store)
    d._runner = mock_runner
    return d


class TestDreamTaskMemoryRouting:
    async def test_task_line_written_to_task_store(self, dream, mock_provider, mock_runner, mock_task_store, store):
        """[TASK:<id>] lines from Phase 1 should be written to task_store."""
        store.append_history("discussed Redis caching", metadata={"task_id": "abc"})
        mock_provider.chat_with_retry.return_value = MagicMock(content=(
            "[USER] user likes spicy food\n"
            "[TASK:abc] need to use Redis for caching\n"
            "[MEMORY] project uses FastAPI"
        ))

        await dream.run()

        mock_task_store.add_task_memory.assert_called_once_with("abc", "need to use Redis for caching")

    async def test_task_line_excluded_from_phase2_prompt(self, dream, mock_provider, mock_runner, mock_task_store, store):
        """[TASK:<id>] lines must NOT appear in the Phase 2 prompt."""
        store.append_history("some work", metadata={"task_id": "abc"})
        mock_provider.chat_with_retry.return_value = MagicMock(content=(
            "[USER] user likes dark mode\n"
            "[TASK:abc] decided to use Postgres\n"
            "[MEMORY] project uses FastAPI"
        ))

        await dream.run()

        phase2_prompt = mock_runner.run.call_args[0][0].initial_messages[-1]["content"]
        assert "[TASK:abc]" not in phase2_prompt

    async def test_global_lines_still_reach_phase2(self, dream, mock_provider, mock_runner, mock_task_store, store):
        """[USER] and [MEMORY] lines must still be passed to Phase 2 unchanged."""
        store.append_history("some work", metadata={"task_id": "abc"})
        mock_provider.chat_with_retry.return_value = MagicMock(content=(
            "[USER] user likes dark mode\n"
            "[TASK:abc] decided to use Postgres\n"
            "[MEMORY] project uses FastAPI"
        ))

        await dream.run()

        phase2_prompt = mock_runner.run.call_args[0][0].initial_messages[-1]["content"]
        assert "[USER] user likes dark mode" in phase2_prompt
        assert "[MEMORY] project uses FastAPI" in phase2_prompt

    async def test_multiple_task_lines_all_written(self, dream, mock_provider, mock_runner, mock_task_store, store):
        """Multiple [TASK:...] lines, including different task IDs, should all be written."""
        store.append_history("multi task work",
                             metadata={"task_id": "abc"})
        mock_provider.chat_with_retry.return_value = MagicMock(content=(
            "[TASK:abc] use Redis for caching\n"
            "[TASK:abc] deadline is next Friday\n"
            "[TASK:xyz] separate task fact"
        ))

        await dream.run()

        calls = {(c.args[0], c.args[1]) for c in mock_task_store.add_task_memory.call_args_list}
        assert ("abc", "use Redis for caching") in calls
        assert ("abc", "deadline is next Friday") in calls
        assert ("xyz", "separate task fact") in calls

    async def test_task_context_injected_into_phase1_when_batch_has_task_id(
            self, dream, mock_provider, mock_runner, mock_task_store, store):
        """When batch entries carry task_id, task summary/memory is fetched and added to Phase 1."""
        mock_task_store.build_task_summary.return_value = "Task T1: build auth module"
        mock_task_store.build_task_memory_context.return_value = "## Task Memory\n- Use JWT"

        store.append_history("auth discussion", metadata={"task_id": "t1"})
        mock_provider.chat_with_retry.return_value = MagicMock(content="[USER] nothing new")

        await dream.run()

        mock_task_store.build_task_summary.assert_called_with("t1")
        mock_task_store.build_task_memory_context.assert_called_with("t1")
        phase1_prompt = mock_provider.chat_with_retry.call_args[1]["messages"][-1]["content"]
        assert "Task T1: build auth module" in phase1_prompt

    async def test_changelog_includes_task_memory_entries(self, dream, mock_provider, mock_runner, mock_task_store, store):
        """task_memory_add actions should appear in the Dream changelog."""
        store.append_history("work item", metadata={"task_id": "abc"})
        mock_provider.chat_with_retry.return_value = MagicMock(content="[TASK:abc] use gRPC")

        result = await dream.run()

        assert result is True
        # changelog is internal but add_task_memory must have been called
        mock_task_store.add_task_memory.assert_called_once_with("abc", "use gRPC")


class TestDreamTaskMemoryNoTaskId:
    async def test_task_store_not_called_when_no_task_id_in_batch(
            self, store, mock_provider, mock_runner, mock_task_store):
        """When batch has no task_id metadata, task_store must not be touched."""
        dream = Dream(store=store, provider=mock_provider, model="test-model",
                      max_batch_size=5, task_store=mock_task_store)
        dream._runner = mock_runner

        store.append_history("user likes coffee")  # no metadata
        mock_provider.chat_with_retry.return_value = MagicMock(content="[USER] user likes coffee")

        await dream.run()

        mock_task_store.add_task_memory.assert_not_called()
        mock_task_store.build_task_summary.assert_not_called()
        mock_task_store.build_task_memory_context.assert_not_called()

    async def test_phase2_gets_full_analysis_when_no_task_id(
            self, store, mock_provider, mock_runner, mock_task_store):
        """Without task_id in batch, Phase 2 should receive the full unfiltered analysis."""
        dream = Dream(store=store, provider=mock_provider, model="test-model",
                      max_batch_size=5, task_store=mock_task_store)
        dream._runner = mock_runner

        store.append_history("some event")  # no metadata
        analysis = "[USER] user prefers vim\n[MEMORY] project uses Django"
        mock_provider.chat_with_retry.return_value = MagicMock(content=analysis)

        await dream.run()

        phase2_prompt = mock_runner.run.call_args[0][0].initial_messages[-1]["content"]
        assert "[USER] user prefers vim" in phase2_prompt
        assert "[MEMORY] project uses Django" in phase2_prompt

    async def test_no_task_store_does_not_crash(self, store, mock_provider, mock_runner):
        """Dream with task_store=None should work normally (no task routing)."""
        dream = Dream(store=store, provider=mock_provider, model="test-model",
                      max_batch_size=5, task_store=None)
        dream._runner = mock_runner

        store.append_history("some event", metadata={"task_id": "abc"})
        mock_provider.chat_with_retry.return_value = MagicMock(content="[USER] likes coffee")

        result = await dream.run()

        assert result is True
        mock_runner.run.assert_called_once()
