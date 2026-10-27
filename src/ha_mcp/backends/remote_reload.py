"""Own the existing remote reload waiter across semantic operations."""

from typing import Any
from uuid import uuid4

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation


class RemoteReloadWatches:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.watches: dict[str, tuple[tuple[str | None, str], Any, Any]] = {}
        self.starting = 0

    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> Any:
        if operation == "config.reload.watch":
            from ha_mcp.tools.config_write_helpers import config_reload_waiter

            if arguments["event_type"] not in {"automation_reloaded", "scene_reloaded"}:
                raise UnsupportedOperation("config.reload.event")
            if len(self.watches) + self.starting >= 32:
                raise ToolFailure("WATCH_LIMIT", "Too many active reload watches.")
            scope = config_reload_waiter(
                self.client,
                arguments["event_type"],
                timeout=min(max(float(arguments.get("timeout", 10)), 0), 30),
            )
            self.starting += 1
            try:
                wait = await scope.__aenter__()
                token = uuid4().hex
                self.watches[token] = ((actor.user_id, actor.client_id), scope, wait)
                return {"watch_id": token}
            finally:
                self.starting -= 1
        token = arguments["watch_id"]
        row = self.watches.get(token)
        if row is None or row[0] != (actor.user_id, actor.client_id):
            raise ToolFailure(
                "WATCH_NOT_OWNED", "This caller does not own the reload watch."
            )
        if operation == "config.reload.wait":
            return await row[2]()
        if operation == "config.reload.close":
            await row[1].__aexit__(None, None, None)
            self.watches.pop(token, None)
            return None
        raise UnsupportedOperation(operation)

    async def stop(self) -> None:
        for token, row in list(self.watches.items()):
            await row[1].__aexit__(None, None, None)
            self.watches.pop(token, None)
