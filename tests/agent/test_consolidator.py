"""Tests for the lightweight Consolidator — append-only to HISTORY.md."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.agent.memory import Consolidator, MemoryStore
from nanobot.session.manager import Session


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def consolidator(store, mock_provider):
    sessions = MagicMock()
    sessions.save = MagicMock()
    return Consolidator(
        store=store,
        provider=mock_provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=1000,
        build_messages=MagicMock(return_value=[]),
        get_tool_definitions=MagicMock(return_value=[]),
        max_completion_tokens=100,
    )


class TestConsolidatorSummarize:
    async def test_summarize_appends_to_history(self, consolidator, mock_provider, store):
        """Consolidator should call LLM to summarize, then append to HISTORY.md."""
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="User fixed a bug in the auth module."
        )
        messages = [
            {"role": "user", "content": "fix the auth bug"},
            {"role": "assistant", "content": "Done, fixed the race condition."},
        ]
        result = await consolidator.archive(messages)
        assert result == "User fixed a bug in the auth module."
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1

    async def test_summarize_raw_dumps_on_llm_failure(self, consolidator, mock_provider, store):
        """On LLM failure, raw-dump messages to HISTORY.md."""
        mock_provider.chat_with_retry.side_effect = Exception("API error")
        messages = [{"role": "user", "content": "hello"}]
        result = await consolidator.archive(messages)
        assert result is None  # no summary on raw dump fallback
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert "[RAW]" in entries[0]["content"]

    async def test_summarize_skips_empty_messages(self, consolidator):
        result = await consolidator.archive([])
        assert result is None


class TestConsolidatorTokenBudget:
    async def test_prompt_below_threshold_does_not_consolidate(self, consolidator):
        """No consolidation when tokens are within budget."""
        session = MagicMock()
        session.last_consolidated = 0
        session.messages = [{"role": "user", "content": "hi"}]
        session.key = "test:key"
        consolidator.estimate_session_prompt_tokens = MagicMock(return_value=(100, "tiktoken"))
        consolidator.archive = AsyncMock(return_value=True)
        await consolidator.maybe_consolidate_by_tokens(session)
        consolidator.archive.assert_not_called()

    async def test_chunk_cap_preserves_user_turn_boundary(self, consolidator):
        """Chunk cap should rewind to the last user boundary within the cap."""
        consolidator._SAFETY_BUFFER = 0
        session = MagicMock()
        session.last_consolidated = 0
        session.key = "test:key"
        session.messages = [
            {
                "role": "user" if i in {0, 50, 61} else "assistant",
                "content": f"m{i}",
            }
            for i in range(70)
        ]
        consolidator.estimate_session_prompt_tokens = MagicMock(
            side_effect=[(1200, "tiktoken"), (400, "tiktoken")]
        )
        consolidator.pick_consolidation_boundary = MagicMock(return_value=(61, 999))
        consolidator.archive = AsyncMock(return_value=True)

        await consolidator.maybe_consolidate_by_tokens(session)

        archived_chunk = consolidator.archive.await_args.args[0]
        assert len(archived_chunk) == 50
        assert archived_chunk[0]["content"] == "m0"
        assert archived_chunk[-1]["content"] == "m49"
        assert session.last_consolidated == 50

    async def test_chunk_cap_skips_when_no_user_boundary_within_cap(self, consolidator):
        """If the cap would cut mid-turn, consolidation should skip that round."""
        consolidator._SAFETY_BUFFER = 0
        session = MagicMock()
        session.last_consolidated = 0
        session.key = "test:key"
        session.messages = [
            {
                "role": "user" if i in {0, 61} else "assistant",
                "content": f"m{i}",
            }
            for i in range(70)
        ]
        consolidator.estimate_session_prompt_tokens = MagicMock(return_value=(1200, "tiktoken"))
        consolidator.pick_consolidation_boundary = MagicMock(return_value=(61, 999))
        consolidator.archive = AsyncMock(return_value=True)

        await consolidator.maybe_consolidate_by_tokens(session)

        consolidator.archive.assert_not_awaited()
        assert session.last_consolidated == 0


class TestConsolidatorMetadata:
    async def test_archive_passes_task_id_from_session(self, consolidator, mock_provider, store):
        """archive() with a session that has task_id should write metadata to history."""
        mock_provider.chat_with_retry.return_value = MagicMock(content="summary text")
        session = Session(key="cli:chat1")
        session.metadata["task_id"] = "task_abc"
        messages = [{"role": "user", "content": "hi"}]

        await consolidator.archive(messages, session=session)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["metadata"]["task_id"] == "task_abc"
        assert entries[0]["metadata"]["session_key"] == "cli:chat1"

    async def test_archive_without_session_has_no_metadata(self, consolidator, mock_provider, store):
        """archive() with no session should not write metadata."""
        mock_provider.chat_with_retry.return_value = MagicMock(content="summary")
        messages = [{"role": "user", "content": "hi"}]

        await consolidator.archive(messages)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert "metadata" not in entries[0]

    async def test_archive_session_without_task_id_omits_task_id(self, consolidator, mock_provider, store):
        """Session with no task_id in metadata should not write task_id."""
        mock_provider.chat_with_retry.return_value = MagicMock(content="summary")
        session = Session(key="cli:chat2")
        messages = [{"role": "user", "content": "hi"}]

        await consolidator.archive(messages, session=session)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        meta = entries[0].get("metadata", {})
        assert "task_id" not in meta

    async def test_archive_fallback_also_passes_metadata(self, consolidator, mock_provider, store):
        """On LLM failure, raw_archive should also receive metadata with task_id."""
        mock_provider.chat_with_retry.side_effect = Exception("API down")
        session = Session(key="cli:x")
        session.metadata["task_id"] = "t1"
        messages = [{"role": "user", "content": "hi"}]

        await consolidator.archive(messages, session=session)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert "[RAW]" in entries[0]["content"]
        assert entries[0]["metadata"]["task_id"] == "t1"
