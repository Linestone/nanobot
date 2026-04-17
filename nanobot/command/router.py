"""Minimal command routing table for slash commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from nanobot.bus.events import InboundMessage, OutboundMessage
    from nanobot.session.manager import Session

Handler = Callable[["CommandContext"], Awaitable["OutboundMessage | None"]]


@dataclass
class CommandContext:
    """Everything a command handler needs to produce a response."""

    msg: InboundMessage
    session: Session | None
    key: str
    raw: str
    args: str = ""
    loop: Any = None


@dataclass
class CommandMetadata:
    """Metadata describing a registered command for agent exposure."""

    pattern: str
    handler: Handler
    description: str = ""
    agent_accessible: bool = False
    agent_description: str = ""
    agent_parameters: dict[str, Any] = field(default_factory=dict)
    confirmation_required: bool = False


class CommandRouter:
    """Pure dict-based command dispatch.

    Three tiers checked in order:
      1. *priority* — exact-match commands handled before the dispatch lock
         (e.g. /stop, /restart).
      2. *exact* — exact-match commands handled inside the dispatch lock.
      3. *prefix* — longest-prefix-first match (e.g. "/team ").
      4. *interceptors* — fallback predicates (e.g. team-mode active check).
    """

    def __init__(self) -> None:
        self._priority: dict[str, Handler] = {}
        self._exact: dict[str, Handler] = {}
        self._prefix: list[tuple[str, Handler]] = []
        self._interceptors: list[Handler] = []
        self._meta: dict[str, CommandMetadata] = {}

    def _register_meta(
        self,
        pattern: str,
        handler: Handler,
        *,
        description: str = "",
        agent_accessible: bool = False,
        agent_description: str = "",
        agent_parameters: dict[str, Any] | None = None,
        confirmation_required: bool = False,
    ) -> None:
        self._meta[pattern] = CommandMetadata(
            pattern=pattern,
            handler=handler,
            description=description,
            agent_accessible=agent_accessible,
            agent_description=agent_description,
            agent_parameters=agent_parameters or {},
            confirmation_required=confirmation_required,
        )

    def priority(
        self,
        cmd: str,
        handler: Handler,
        *,
        description: str = "",
        agent_accessible: bool = False,
        agent_description: str = "",
        agent_parameters: dict[str, Any] | None = None,
        confirmation_required: bool = False,
    ) -> None:
        self._priority[cmd] = handler
        self._register_meta(
            cmd,
            handler,
            description=description,
            agent_accessible=agent_accessible,
            agent_description=agent_description,
            agent_parameters=agent_parameters,
            confirmation_required=confirmation_required,
        )

    def exact(
        self,
        cmd: str,
        handler: Handler,
        *,
        description: str = "",
        agent_accessible: bool = False,
        agent_description: str = "",
        agent_parameters: dict[str, Any] | None = None,
        confirmation_required: bool = False,
    ) -> None:
        self._exact[cmd] = handler
        self._register_meta(
            cmd,
            handler,
            description=description,
            agent_accessible=agent_accessible,
            agent_description=agent_description,
            agent_parameters=agent_parameters,
            confirmation_required=confirmation_required,
        )

    def prefix(
        self,
        pfx: str,
        handler: Handler,
        *,
        description: str = "",
        agent_accessible: bool = False,
        agent_description: str = "",
        agent_parameters: dict[str, Any] | None = None,
        confirmation_required: bool = False,
    ) -> None:
        self._prefix.append((pfx, handler))
        self._prefix.sort(key=lambda p: len(p[0]), reverse=True)
        self._register_meta(
            pfx,
            handler,
            description=description,
            agent_accessible=agent_accessible,
            agent_description=agent_description,
            agent_parameters=agent_parameters,
            confirmation_required=confirmation_required,
        )

    def intercept(self, handler: Handler) -> None:
        self._interceptors.append(handler)

    def is_priority(self, text: str) -> bool:
        return text.strip().lower() in self._priority

    def get_agent_accessible_commands(self) -> list[CommandMetadata]:
        """Return all commands marked as agent_accessible."""
        return [m for m in self._meta.values() if m.agent_accessible]

    def get_command_metadata(self, pattern: str) -> CommandMetadata | None:
        """Return metadata for a specific command pattern."""
        return self._meta.get(pattern)

    async def dispatch_priority(self, ctx: CommandContext) -> OutboundMessage | None:
        """Dispatch a priority command. Called from run() without the lock."""
        handler = self._priority.get(ctx.raw.lower())
        if handler:
            return await handler(ctx)
        return None

    async def dispatch(self, ctx: CommandContext) -> OutboundMessage | None:
        """Try exact, prefix, then interceptors. Returns None if unhandled."""
        cmd = ctx.raw.lower()

        if handler := self._exact.get(cmd):
            return await handler(ctx)

        for pfx, handler in self._prefix:
            if cmd.startswith(pfx):
                ctx.args = ctx.raw[len(pfx):]
                return await handler(ctx)

        for interceptor in self._interceptors:
            result = await interceptor(ctx)
            if result is not None:
                return result

        return None
