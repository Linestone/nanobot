"""Generic bridge tool: let the agent execute slash commands registered as agent_accessible."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.bus.events import InboundMessage
from nanobot.command.router import CommandRouter

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


@tool_parameters(
    tool_parameters_schema(
        command=StringSchema(
            "The slash command to execute (e.g. '/task create'). "
            "Must be one of the agent-accessible commands listed in the tool description.",
        ),
        args=StringSchema(
            "Raw argument string passed to the command exactly as a user would type it. "
            "For example: '--parent abc123 Fix login bug | This is the description'",
            nullable=True,
        ),
        required=["command"],
    )
)
class RunCommandTool(Tool):
    """Execute a nanobot slash command on behalf of the agent.

    Only commands explicitly marked as *agent_accessible* can be invoked.
    The available command list is injected into this tool's description dynamically
    so the LLM always sees the up-to-date allowlist.
    """

    def __init__(
        self,
        router: CommandRouter,
        loop: AgentLoop | None = None,
    ) -> None:
        self._router = router
        self._loop = loop
        self._session_key: str | None = None
        self._channel: str = "cli"
        self._chat_id: str = "direct"
        self._message_id: str | None = None
        self._metadata: dict[str, Any] | None = None

    def set_context(
        self,
        session_key: str | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._session_key = session_key
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._metadata = metadata

    @property
    def name(self) -> str:
        return "run_command"

    def _display_name(self, pattern: str) -> str:
        """Strip trailing space from prefix patterns for LLM-friendly display."""
        return pattern.rstrip()

    def _resolve_pattern(self, command: str) -> str | None:
        """Map a user-supplied command to the internal router pattern.

        Handles the common case where prefix commands are registered with a
        trailing space (e.g. '/task create ') but the LLM passes '/task create'.
        """
        accessible = self._router.get_agent_accessible_commands()
        allowed_patterns = {m.pattern for m in accessible}
        if command in allowed_patterns:
            return command
        # Prefix patterns often end with a space — try appending one.
        spaced = command + " "
        if spaced in allowed_patterns:
            return spaced
        return None

    @property
    def description(self) -> str:
        accessible = self._router.get_agent_accessible_commands()
        if not accessible:
            return (
                "Execute a nanobot slash command. "
                "No commands are currently exposed to the agent."
            )

        lines: list[str] = [
            "Execute a nanobot slash command on behalf of the agent.",
            "",
            "Available commands:",
        ]
        for meta in sorted(accessible, key=lambda m: m.pattern):
            display = self._display_name(meta.pattern)
            desc = meta.agent_description or meta.description or "No description"
            lines.append(f"- `{display}`: {desc}")
            if meta.confirmation_required:
                lines.append("  (destructive — requires explicit reasoning before use)")
            if meta.agent_parameters:
                for param_name, param_schema in meta.agent_parameters.items():
                    req = "required" if param_schema.get("required") else "optional"
                    param_desc = param_schema.get("description", "")
                    lines.append(f"    - `{param_name}` ({req}): {param_desc}")
        lines.append("")
        lines.append(
            "When invoking a command, pass the full command text (e.g. '/task create') "
            "in the `command` field and the remaining arguments in `args`."
        )
        return "\n".join(lines)

    async def execute(self, command: str, args: str | None = None) -> str:
        resolved = self._resolve_pattern(command)
        if resolved is None:
            return f"Error: command '{command}' is not agent-accessible."

        meta = self._router.get_command_metadata(resolved)
        if meta is None:
            return f"Error: no metadata found for '{command}'."

        full_raw = f"{resolved}{args}".strip() if args else resolved

        msg = InboundMessage(
            channel=self._channel,
            sender_id="agent",
            chat_id=self._chat_id,
            content=full_raw,
            metadata={**(self._metadata or {})},
        )
        from nanobot.command.router import CommandContext

        session = None
        if self._loop is not None and self._session_key is not None:
            session = self._loop.sessions.get_or_create(self._session_key)

        ctx = CommandContext(
            msg=msg,
            session=session,
            key=self._session_key or f"{self._channel}:{self._chat_id}",
            raw=full_raw,
            loop=self._loop,
        )

        result = await self._router.dispatch(ctx)
        if result is None:
            return f"Error: command '{command}' was not handled by the router."

        if meta.confirmation_required:
            return f"[Destructive operation executed]\n{result.content}"
        return result.content
