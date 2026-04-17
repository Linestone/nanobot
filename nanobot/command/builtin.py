"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys

import shlex

from nanobot import __version__
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.task import TaskTreeStore
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(channel=msg.channel, chat_id=msg.chat_id)

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    try:
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    except Exception:
        pass
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)
    
    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    try:
        from nanobot.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    except Exception:
        pass  # Never let usage fetch break /status
    active_tasks = loop._active_tasks.get(ctx.key, [])
    task_count = sum(1 for t in active_tasks if not t.done())
    try:
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    except Exception:
        pass
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
            active_task_count=task_count,
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot:
        loop._schedule_background(loop.consolidator.archive(snapshot))
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    async def _run_dream():
        t0 = time.monotonic()
        try:
            did_work = await loop.dream.run()
            elapsed = time.monotonic() - t0
            if did_work:
                content = f"Dream completed in {elapsed:.1f}s."
            else:
                content = "Dream: nothing to process."
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest commit (HEAD~1 vs HEAD).
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest commit's diff
        commits = git.log(max_entries=1)
        result = git.show_commit_diff(commits[0].sha) if commits else None
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent commits for the user to pick
        commits = git.log(max_entries=10)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
        new_sha = git.revert(sha)
        if new_sha:
            content = (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            )
        else:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "It may not exist, or it may be the first saved version with no earlier state to restore."
            )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _task_parse_title_description(raw: str) -> tuple[str, str]:
    parts = [part.strip() for part in raw.split("|", 1)]
    title = parts[0] if parts else ""
    description = parts[1] if len(parts) > 1 else ""
    return title, description


def _task_parse_create_args(raw: str) -> tuple[str, str, str | None]:
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    parent_id: str | None = None
    remaining: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "--parent":
            if i + 1 < len(tokens):
                parent_id = tokens[i + 1]
                i += 2
                continue
        remaining.append(tokens[i])
        i += 1

    title, description = _task_parse_title_description(" ".join(remaining))
    return title, description, parent_id


def _task_parse_update_args(raw: str) -> tuple[str, dict[str, str | None]]:
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    task_id = tokens[0] if tokens else ""
    updates: dict[str, str | None] = {
        "title": None,
        "description": None,
        "status": None,
        "parent_id": None,
    }
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--title" and i + 1 < len(tokens):
            updates["title"] = tokens[i + 1]
            i += 2
            continue
        if token == "--description" and i + 1 < len(tokens):
            updates["description"] = tokens[i + 1]
            i += 2
            continue
        if token == "--status" and i + 1 < len(tokens):
            updates["status"] = tokens[i + 1].lower()
            i += 2
            continue
        if token == "--parent" and i + 1 < len(tokens):
            updates["parent_id"] = tokens[i + 1]
            i += 2
            continue
        i += 1
    return task_id, updates


def _task_help_text() -> str:
    lines = [
        "🐈 nanobot task commands:",
        "/task create [--parent <parent_id>] <title> | <description> — Create a new task",
        "/task update <task_id> [--title <title>] [--description <desc>] [--status <status>] [--parent <parent_id>] — Update a task",
        "/task status <task_id> <status> — Update task status (todo, doing, done, blocked)",
        "/task list [status] — List tasks, optionally filtered by status",
        "/task show <task_id> — Show a task's details",
        "/task tree [<task_id>] — Show the task tree or subtree",
        "/task children <task_id> — Show direct children of a task",
        "/task move <task_id> <parent_id> — Move a task under a new parent",
        "/task delete <task_id> — Delete a task and orphan its children",
        "/task memory add <task_id> | <content> — Add a task-specific memory entry",
        "/task memory view <task_id> — View task-specific memory entries",
        "/task attach <task_id> — Attach this session to a task",
        "/task detach — Detach the current task from this session",
    ]
    return "\n".join(lines)


async def cmd_task_help(ctx: CommandContext) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=_task_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_create(ctx: CommandContext) -> OutboundMessage:
    args = ctx.args.strip()
    if not args:
        return await cmd_task_help(ctx)

    title, description, parent_id = _task_parse_create_args(args)
    if not title:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Please provide a task title. Usage: /task create [--parent <parent_id>] <title> | <description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    store = TaskTreeStore(ctx.loop.workspace)
    if parent_id:
        parent = store.get_task(parent_id)
        if not parent:
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content=f"Parent task `{parent_id}` not found.",
                metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
            )
    task = store.create_task(title=title, description=description, parent_id=parent_id)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(
            f"Created task `{task.id}`:\n"
            f"- Title: {task.title}\n"
            f"- Status: {task.status}\n"
            f"- Type: {task.task_type}\n"
            f"- Description: {task.description or '(none)'}"
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_list(ctx: CommandContext) -> OutboundMessage:
    status = ctx.args.strip().lower() or None
    store = TaskTreeStore(ctx.loop.workspace)
    tasks = store.list_tasks(status=status)
    if not tasks:
        content = "No tasks found." if not status else f"No tasks found with status '{status}'."
    else:
        lines = ["Tasks:"]
        for task in tasks:
            lines.append(f"- `{task.id}` {task.title} [{task.status}] ({task.task_type})")
        content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_show(ctx: CommandContext) -> OutboundMessage:
    task_id = ctx.args.strip()
    if not task_id:
        return await cmd_task_help(ctx)
    store = TaskTreeStore(ctx.loop.workspace)
    task = store.get_task(task_id)
    if not task:
        content = f"Task `{task_id}` not found."
    else:
        content = store.build_task_summary(task.id) or f"Task `{task_id}` not found."
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _task_parse_memory_add_args(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if raw.startswith("add "):
        raw = raw[len("add "):].strip()
    if not raw:
        return "", ""
    if "|" in raw:
        left, right = raw.split("|", 1)
        task_id = left.strip().split(None, 1)[0] if left.strip().split(None, 1) else ""
        content = right.strip()
    else:
        parts = raw.split(None, 1)
        task_id = parts[0] if parts else ""
        content = parts[1].strip() if len(parts) > 1 else ""
    return task_id, content


async def cmd_task_memory_add(ctx: CommandContext) -> OutboundMessage:
    task_id, content = _task_parse_memory_add_args(ctx.args)
    if not task_id or not content:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /task memory add <task_id> | <content>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    store = TaskTreeStore(ctx.loop.workspace)
    if not store.get_task(task_id):
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Task `{task_id}` not found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    store.add_task_memory(task_id, content)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Added task memory to `{task_id}`.",
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_memory_view(ctx: CommandContext) -> OutboundMessage:
    task_id = ctx.args.strip()
    if task_id.startswith("view "):
        task_id = task_id[len("view "):].strip()
    if not task_id:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /task memory view <task_id>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    store = TaskTreeStore(ctx.loop.workspace)
    if not store.get_task(task_id):
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Task `{task_id}` not found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    context = store.build_task_memory_context(task_id)
    if not context:
        content = f"Task `{task_id}` has no task memory entries."
    else:
        content = context
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_attach(ctx: CommandContext) -> OutboundMessage:
    task_id = ctx.args.strip()
    if not task_id:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Please provide a task ID to attach this session to. Usage: /task attach <task_id>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    store = TaskTreeStore(ctx.loop.workspace)
    task = store.get_task(task_id)
    if not task:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Task `{task_id}` not found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if not ctx.session:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Unable to attach task because this command requires an active session.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    ctx.session.metadata["task_id"] = task.id
    ctx.loop.sessions.save(ctx.session)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Attached current session to task `{task.title}` (`{task.id}`).",
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_detach(ctx: CommandContext) -> OutboundMessage:
    if not ctx.session:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Unable to detach task because this command requires an active session.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if "task_id" not in ctx.session.metadata:
        content = "No task is currently attached to this session."
    else:
        ctx.session.metadata.pop("task_id", None)
        ctx.loop.sessions.save(ctx.session)
        content = "Detached the current task from this session."
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_status(ctx: CommandContext) -> OutboundMessage:
    raw_args = ctx.args.strip().split(None, 1)
    if len(raw_args) < 2:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /task status <task_id> <status>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    task_id, status = raw_args[0], raw_args[1].strip().lower()
    if status not in {"todo", "doing", "done", "blocked"}:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Invalid status. Valid statuses are: todo, doing, done, blocked.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    store = TaskTreeStore(ctx.loop.workspace)
    task = store.update_task(task_id, status=status)
    if not task:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Task `{task_id}` not found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Updated task `{task.id}` to status `{task.status}`.",
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_update(ctx: CommandContext) -> OutboundMessage:
    raw_args = ctx.args.strip()
    task_id, updates = _task_parse_update_args(raw_args)
    if not task_id:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /task update <task_id> [--title <title>] [--description <desc>] [--status <status>] [--parent <parent_id>]",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    if all(value is None for value in updates.values()):
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Please provide at least one field to update: --title, --description, --status, or --parent.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    store = TaskTreeStore(ctx.loop.workspace)
    task = store.get_task(task_id)
    if not task:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Task `{task_id}` not found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    if updates["status"] and updates["status"] not in {"todo", "doing", "done", "blocked"}:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Invalid status. Valid statuses are: todo, doing, done, blocked.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    if updates["parent_id"]:
        parent_id = updates["parent_id"]
        if parent_id == task_id:
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content="A task cannot be its own parent.",
                metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
            )
        if store.is_descendant(parent_id, task_id):
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content="Cannot move a task under its own descendant.",
                metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
            )
        if not store.get_task(parent_id):
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content=f"Parent task `{parent_id}` not found.",
                metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
            )

    updated = store.update_task(task_id, **{k: v for k, v in updates.items() if v is not None})
    if not updated:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Failed to update task `{task_id}`.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    changes = [f"{key}={value}" for key, value in updates.items() if value is not None]
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(f"Updated task `{task_id}`." if not changes else f"Updated task `{task_id}`: " + ", ".join(changes)),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_children(ctx: CommandContext) -> OutboundMessage:
    task_id = ctx.args.strip()
    if not task_id:
        return await cmd_task_help(ctx)
    store = TaskTreeStore(ctx.loop.workspace)
    task = store.get_task(task_id)
    if not task:
        content = f"Task `{task_id}` not found."
    else:
        children = store.get_children(task_id)
        if not children:
            content = f"Task `{task_id}` has no direct children."
        else:
            lines = [f"Children of `{task.id}` {task.title}:"]
            lines.extend([f"- `{child.id}` {child.title} [{child.status}]" for child in children])
            content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_tree(ctx: CommandContext) -> OutboundMessage:
    task_id = ctx.args.strip() or None
    store = TaskTreeStore(ctx.loop.workspace)
    content = store.build_task_tree(task_id)
    if content is None:
        content = f"Task `{ctx.args.strip()}` not found."
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content or "No tasks found.",
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_move(ctx: CommandContext) -> OutboundMessage:
    raw_args = ctx.args.strip().split(None, 2)
    if len(raw_args) < 2:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /task move <task_id> <parent_id>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    task_id, parent_id = raw_args[0], raw_args[1]
    store = TaskTreeStore(ctx.loop.workspace)
    task = store.get_task(task_id)
    if not task:
        content = f"Task `{task_id}` not found."
    elif task_id == parent_id:
        content = "A task cannot be its own parent."
    elif store.is_descendant(parent_id, task_id):
        content = "Cannot move a task under its own descendant."
    elif not store.get_task(parent_id):
        content = f"Parent task `{parent_id}` not found."
    else:
        updated = store.update_task(task_id, parent_id=parent_id)
        content = f"Moved task `{task_id}` under parent `{parent_id}`." if updated else f"Failed to move task `{task_id}`."
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_task_delete(ctx: CommandContext) -> OutboundMessage:
    task_id = ctx.args.strip()
    if not task_id:
        return await cmd_task_help(ctx)
    store = TaskTreeStore(ctx.loop.workspace)
    task = store.get_task(task_id)
    if not task:
        content = f"Task `{task_id}` not found."
    elif store.delete_task(task_id):
        content = f"Deleted task `{task_id}`. Its direct children are now orphaned."
    else:
        content = f"Failed to delete task `{task_id}`."
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = [
        "🐈 nanobot commands:",
        "/new — Start a new conversation",
        "/stop — Stop the current task",
        "/restart — Restart the bot",
        "/status — Show bot status",
        "/dream — Manually trigger Dream consolidation",
        "/dream-log — Show what the last Dream changed",
        "/dream-restore — Revert memory to a previous state",
        "/export-context [filename] — Export current session context to a Markdown file",
        "",
        "Task commands:",
        "/task — Show task command help",
        "/task create [--parent <parent_id>] <title> | <description> — Create a new task",
        "/task update <task_id> [--title <title>] [--description <desc>] [--status <status>] [--parent <parent_id>] — Update a task",
        "/task status <task_id> <status> — Update task status (todo, doing, done, blocked)",
        "/task list [status] — List tasks, optionally filtered by status",
        "/task show <task_id> — Show a task's details",
        "/task tree [<task_id>] — Show the task tree or subtree",
        "/task children <task_id> — Show direct children of a task",
        "/task move <task_id> <parent_id> — Move a task under a new parent",
        "/task delete <task_id> — Delete a task and orphan its children",
        "/task memory add <task_id> | <content> — Add a task-specific memory entry",
        "/task memory view <task_id> — View task-specific memory entries",
        "/task attach <task_id> — Attach this session to a task",
        "/task detach — Detach the current task from this session",
        "",
        "Session commands:",
        "/session list — List all sessions",
        "/session switch <name> — Switch to an existing session",
        "/session new [name] — Create and switch to a new session",
        "",
        "/help — Show available commands",
    ]
    return "\n".join(lines)


def _resolve_session(ctx: CommandContext, target: str) -> str | None:
    """Resolve target (title, key suffix, or full key) to a session key. Returns None if not found."""
    mgr = ctx.loop.sessions
    # Full key match
    if mgr._get_session_path(target).exists():
        return target
    # channel:target key match
    candidate = f"{ctx.msg.channel}:{target}"
    if mgr._get_session_path(candidate).exists():
        return candidate
    # Title match (case-insensitive, first match wins)
    lower = target.lower()
    for info in mgr.list_sessions():
        if info.get("title", "").lower() == lower:
            return info["key"]
    return None


def _session_label(key: str, title: str) -> str:
    """Return the display label for a session: title if set, else key suffix."""
    return title if title else (key.split(":", 1)[-1] if ":" in key else key)


async def cmd_session_list(ctx: CommandContext) -> OutboundMessage:
    """List all sessions with their attached task (if any)."""
    loop = ctx.loop
    session_infos = loop.sessions.list_sessions()
    if not session_infos:
        content = "No sessions found."
    else:
        store = TaskTreeStore(loop.workspace)
        lines = ["## Sessions", ""]
        current_key = ctx.msg.metadata.get("_current_session_key", ctx.key) if ctx.msg.metadata else ctx.key
        for info in session_infos:
            s_key = info["key"]
            s_title = info.get("title", "")
            is_current = s_key == current_key
            s = loop.sessions.get_or_create(s_key)
            task_id = s.metadata.get("task_id")
            if task_id:
                task = store.get_task(task_id)
                task_label = f" → {task.title} ({task_id})" if task else f" → task:{task_id}"
            else:
                task_label = ""
            current_marker = " <当前会话>" if is_current else ""
            key_suffix = s_key.split(":", 1)[-1] if ":" in s_key else s_key
            if s_title:
                lines.append(f"- {s_title} ({key_suffix}){task_label}{current_marker}")
            else:
                lines.append(f"- {key_suffix}{task_label}{current_marker}")
        content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_session_switch(ctx: CommandContext) -> OutboundMessage:
    """Switch to an existing session by title or key."""
    target = ctx.args.strip()
    if not target:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /session switch <title_or_key>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    new_key = _resolve_session(ctx, target)
    if not new_key:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Session `{target}` not found. Use `/session list` to see existing sessions.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    session = ctx.loop.sessions.get_or_create(new_key)
    label = _session_label(new_key, session.title)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Switched to session **{label}**.",
        metadata={
            **dict(ctx.msg.metadata or {}),
            "render_as": "text",
            "_session_switch": new_key,
        },
    )


async def cmd_session_new(ctx: CommandContext) -> OutboundMessage:
    """Create a new session with an auto-generated key; optional title argument."""
    import uuid
    title = ctx.args.strip()
    new_key = f"{ctx.msg.channel}:{uuid.uuid4().hex[:8]}"
    session = ctx.loop.sessions.get_or_create(new_key)
    if title:
        session.title = title
    ctx.loop.sessions.save(session)
    label = _session_label(new_key, session.title)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Created and switched to session **{label}**.",
        metadata={
            **dict(ctx.msg.metadata or {}),
            "render_as": "text",
            "_session_switch": new_key,
        },
    )


async def cmd_session_rename(ctx: CommandContext) -> OutboundMessage:
    """Rename (set title of) the current session."""
    new_title = ctx.args.strip()
    if not new_title:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /session rename <new_title>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    current_key = ctx.msg.metadata.get("_current_session_key", ctx.key) if ctx.msg.metadata else ctx.key
    session = ctx.loop.sessions.get_or_create(current_key)
    session.title = new_title
    ctx.loop.sessions.save(session)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Session renamed to **{new_title}**.",
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_session_delete(ctx: CommandContext) -> OutboundMessage:
    """Delete a session by title or key. Cannot delete the current session."""
    target = ctx.args.strip()
    if not target:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /session delete <title_or_key>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    del_key = _resolve_session(ctx, target)
    if not del_key:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Session `{target}` not found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    current_key = ctx.msg.metadata.get("_current_session_key", ctx.key) if ctx.msg.metadata else ctx.key
    if del_key == current_key:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Cannot delete the current session. Switch to another session first.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    session = ctx.loop.sessions.get_or_create(del_key)
    label = _session_label(del_key, session.title)
    ctx.loop.sessions.delete(del_key)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Session **{label}** deleted.",
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_session_show(ctx: CommandContext) -> OutboundMessage:
    """Show details of the current or specified session."""
    target = ctx.args.strip()
    current_key = ctx.msg.metadata.get("_current_session_key", ctx.key) if ctx.msg.metadata else ctx.key

    if target:
        show_key = _resolve_session(ctx, target)
        if not show_key:
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content=f"Session `{target}` not found.",
                metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
            )
    else:
        show_key = current_key

    session = ctx.loop.sessions.get_or_create(show_key)
    key_suffix = show_key.split(":", 1)[-1] if ":" in show_key else show_key
    is_current = show_key == current_key

    store = TaskTreeStore(ctx.loop.workspace)
    task_id = session.metadata.get("task_id")
    task_line = ""
    if task_id:
        task = store.get_task(task_id)
        task_line = f"\n**Task:** {task.title} ({task_id})" if task else f"\n**Task:** {task_id}"

    lines = [f"## Session: {_session_label(show_key, session.title)}"]
    if is_current:
        lines[0] += " (current)"
    title_display = session.title if session.title else "(未设置)"
    lines += [
        "",
        f"**Key:** `{show_key}`",
        f"**Title:** {title_display}",
        f"**Messages:** {len(session.messages)}",
        f"**Created:** {session.created_at.strftime('%Y-%m-%d %H:%M')}",
        f"**Updated:** {session.updated_at.strftime('%Y-%m-%d %H:%M')}",
    ]
    if task_line:
        lines.append(task_line.strip())
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_session_help(ctx: CommandContext) -> OutboundMessage:
    content = "\n".join([
        "## Session Commands",
        "",
        "/session list — List all sessions",
        "/session new [title] — Create a new session (auto-generated key, optional title)",
        "/session switch <title_or_key> — Switch to an existing session",
        "/session rename <new_title> — Rename the current session",
        "/session show [title_or_key] — Show session details",
        "/session delete <title_or_key> — Delete a session",
    ])
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _render_message_content(content: object) -> str:
    """Render a message's content field as plain text (strips image blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    src: str = block.get("image_url", {}).get("url", "")
                    label = src[:40] + "…" if len(src) > 40 else src
                    parts.append(f"[image: {label}]")
                else:
                    import json as _json
                    parts.append(_json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _format_context_as_markdown(messages: list[dict], session_key: str) -> str:
    """Format a message list (system + history) as human-readable Markdown."""
    from datetime import datetime
    lines = [
        "# Exported Context",
        "",
        f"**Session:** `{session_key}`  ",
        f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
    ]
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []
        tool_call_id = msg.get("tool_call_id")
        tool_name = msg.get("name")

        if role == "system":
            heading = f"## [{i}] System"
        elif role == "user":
            heading = f"## [{i}] User"
        elif role == "assistant":
            heading = f"## [{i}] Assistant"
        elif role == "tool":
            heading = f"## [{i}] Tool Result (`{tool_name or tool_call_id or 'unknown'}`)"
        else:
            heading = f"## [{i}] {role.capitalize()}"

        lines.append(heading)
        lines.append("")

        if content:
            lines.append(_render_message_content(content))
            lines.append("")

        for tc in tool_calls:
            import json as _json
            tc_name = tc.get("function", {}).get("name") or tc.get("name", "?")
            try:
                raw_args = tc.get("function", {}).get("arguments") or tc.get("arguments") or "{}"
                args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                args_str = _json.dumps(args, indent=2, ensure_ascii=False)
            except Exception:
                args_str = str(tc)
            lines.append(f"**Tool call:** `{tc_name}`")
            lines.append(f"```json\n{args_str}\n```")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


async def cmd_export_context(ctx: CommandContext) -> OutboundMessage:
    """Export the full session context (as seen by the LLM) to a Markdown file.

    Usage:
        /export-context              — write to exported-context.md in workspace
        /export-context <filename>   — write to <filename> in workspace
    """
    filename = ctx.args.strip() or "exported-context.md"
    if not filename.endswith(".md"):
        filename += ".md"

    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    task_id = session.metadata.get("task_id")
    history = session.get_history(max_messages=0)

    messages = loop.context.build_messages(
        history=history,
        current_message="[export-context: no active message]",
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        task_id=task_id,
    )

    markdown = _format_context_as_markdown(messages, ctx.key)
    out_path = loop.workspace / filename
    out_path.write_text(markdown, encoding="utf-8")

    token_count, counter_name = loop.consolidator.estimate_session_prompt_tokens(session)
    size_kb = out_path.stat().st_size / 1024

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(
            f"Context exported to `{out_path}`\n"
            f"- Messages: {len(messages)} ({len(history)} history + 1 system)\n"
            f"- Estimated tokens: {token_count:,} ({counter_name})\n"
            f"- File size: {size_kb:.1f} KB"
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/dream", cmd_dream,
                 agent_accessible=True,
                 agent_description="Manually trigger Dream memory consolidation for the current session.")
    router.exact("/dream-log", cmd_dream_log,
                 agent_accessible=True,
                 agent_description="Show what the last Dream consolidation changed.")
    router.prefix("/dream-log ", cmd_dream_log,
                  agent_accessible=True,
                  agent_description="Show Dream log for a specific number of entries.",
                  agent_parameters={"n": {"type": "integer", "description": "Number of entries to show"}})
    router.exact("/dream-restore", cmd_dream_restore,
                 agent_accessible=True,
                 confirmation_required=True,
                 agent_description="Revert memory to a previous Dream checkpoint.")
    router.prefix("/dream-restore ", cmd_dream_restore,
                  agent_accessible=True,
                  confirmation_required=True,
                  agent_description="Revert memory to a specific checkpoint ID.",
                  agent_parameters={"checkpoint_id": {"type": "string", "description": "Checkpoint ID to restore"}})
    router.exact("/task", cmd_task_help)
    router.exact("/task help", cmd_task_help)
    router.prefix("/task create ", cmd_task_create,
                  agent_accessible=True,
                  agent_description="Create a new task. Optional parent task, title, and description (separated by '|').",
                  agent_parameters={
                      "args": {"type": "string", "description": "e.g. '--parent <id> Title | Description'", "required": True}
                  })
    router.exact("/task list", cmd_task_list,
                 agent_accessible=True,
                 agent_description="List all tasks, optionally filtered by status.")
    router.prefix("/task list ", cmd_task_list,
                  agent_accessible=True,
                  agent_description="List tasks filtered by a specific status.",
                  agent_parameters={"status": {"type": "string", "description": "Filter by status: todo, doing, done, blocked"}})
    router.prefix("/task show ", cmd_task_show,
                  agent_accessible=True,
                  agent_description="Show detailed information about a task.",
                  agent_parameters={"task_id": {"type": "string", "description": "Task ID to show", "required": True}})
    router.prefix("/task memory add ", cmd_task_memory_add,
                  agent_accessible=True,
                  agent_description="Add a memory entry to a task. Format: <task_id> | <content>",
                  agent_parameters={
                      "args": {"type": "string", "description": "e.g. 'task_abc123 | Fixed the auth bug'", "required": True}
                  })
    router.prefix("/task memory view ", cmd_task_memory_view,
                  agent_accessible=True,
                  agent_description="View memory entries for a task.",
                  agent_parameters={"task_id": {"type": "string", "description": "Task ID", "required": True}})
    router.prefix("/task children ", cmd_task_children,
                  agent_accessible=True,
                  agent_description="List direct children of a task.",
                  agent_parameters={"task_id": {"type": "string", "description": "Task ID", "required": True}})
    router.prefix("/task tree", cmd_task_tree,
                  agent_accessible=True,
                  agent_description="Show the task hierarchy tree. Optionally pass a root task ID.")
    router.prefix("/task move ", cmd_task_move,
                  agent_accessible=True,
                  agent_description="Move a task under a different parent.",
                  agent_parameters={
                      "task_id": {"type": "string", "description": "Task to move", "required": True},
                      "parent_id": {"type": "string", "description": "New parent task ID", "required": True},
                  })
    router.prefix("/task delete ", cmd_task_delete,
                  agent_accessible=True,
                  confirmation_required=True,
                  agent_description="Delete a task. Its direct children become orphaned.",
                  agent_parameters={"task_id": {"type": "string", "description": "Task ID to delete", "required": True}})
    router.prefix("/task attach ", cmd_task_attach,
                  agent_accessible=True,
                  agent_description="Attach the current session to a task. All subsequent task memory operations bind to this task.",
                  agent_parameters={"task_id": {"type": "string", "description": "Task ID to attach", "required": True}})
    router.exact("/task detach", cmd_task_detach,
                 agent_accessible=True,
                 agent_description="Detach the current session from its bound task.")
    router.prefix("/task status ", cmd_task_status,
                  agent_accessible=True,
                  agent_description="Update the status of a task.",
                  agent_parameters={
                      "task_id": {"type": "string", "description": "Task ID", "required": True},
                      "status": {"type": "string", "description": "New status: todo, doing, done, blocked", "required": True},
                  })
    router.prefix("/task update ", cmd_task_update,
                  agent_accessible=True,
                  agent_description="Update task fields: --title, --description, --status, --parent.",
                  agent_parameters={
                      "task_id": {"type": "string", "description": "Task ID", "required": True},
                      "args": {"type": "string", "description": "e.g. '--title New Title --status done'", "required": True},
                  })
    router.exact("/session", cmd_session_help)
    router.exact("/session list", cmd_session_list,
                 agent_accessible=True,
                 agent_description="List all sessions.")
    router.prefix("/session switch ", cmd_session_switch,
                  agent_accessible=True,
                  agent_description="Switch to a different session by key.",
                  agent_parameters={"session_key": {"type": "string", "description": "Session key to switch to", "required": True}})
    router.prefix("/session new", cmd_session_new)
    router.prefix("/session rename", cmd_session_rename)
    router.prefix("/session delete ", cmd_session_delete)
    router.prefix("/session show", cmd_session_show,
                  agent_accessible=True,
                  agent_description="Show details of the current session.")
    router.exact("/export-context", cmd_export_context)
    router.prefix("/export-context ", cmd_export_context)
    router.exact("/help", cmd_help)
