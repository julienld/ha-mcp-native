"""Real HTTP bridge probes in an interpreter without collection-time Core stubs."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.mcp import MCPError
from ha_mcp.native_proxy import create_native_component_proxy
from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolFailure

from ._native_approval_probe import _component_imports


class Probe:
    def __init__(self, case: str) -> None:
        self.native = _component_imports()
        self.case = case
        self.valid = True
        self.mutations: list[str] = []
        self.completed = asyncio.Event()
        self.schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }
        self.manager = SimpleNamespace(
            engine=Engine(SimpleNamespace(authorize=self.authorize))
        )
        self.backend = FastMCP(
            "Canonical Home Assistant", instructions="Canonical instructions"
        )
        self.register(
            ToolDefinition(
                "native_read",
                "Native read",
                self.schema,
                self.read,
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            )
        )
        self.register(
            ToolDefinition(
                "native_write",
                "Native write",
                {"type": "object", "properties": {"fail": {"type": "boolean"}}},
                self.write,
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            )
        )
        self.backend.resource("skill://test/SKILL.md")(self.skill)
        self.backend.prompt(self.help_me)
        self.asgi = self.backend.http_app(
            path="/api/webhook/test", stateless_http=True, host_origin_protection=False
        )
        self.lifespan_state: dict = {}

    def register(self, definition: ToolDefinition) -> None:
        self.manager.engine.register(definition)
        self.backend.add_tool(self.native._project_tool(self.manager, definition))

    @staticmethod
    async def authorize(actor: CallContext) -> None:
        if actor.credential:
            await actor.credential()

    @staticmethod
    async def read(args: dict, actor: CallContext) -> dict:
        return {"value": args["value"], "actor": actor.user_id}

    async def write(self, args: dict, actor: CallContext) -> dict:
        self.mutations.append(actor.user_id or "missing")
        self.completed.set()
        if args.get("fail"):
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Completion failed after write",
                status="outcome_unknown",
            )
        return {"saved": True}

    @staticmethod
    def skill() -> str:
        return "Canonical skill content"

    @staticmethod
    def help_me() -> str:
        return "Canonical prompt"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if (
            request.headers.get("Authorization") != "Bearer test-credential"
            or not self.valid
        ):
            return web.Response(status=401)

        async def validate() -> None:
            if not self.valid:
                raise PermissionError("Revoked")

        actor = CallContext("administrator", "trusted-client", credential=validate)
        if self.case != "drop":
            return await self.native.serve_asgi(
                self.asgi,
                request,
                lifespan_state=self.lifespan_state,
                request_state={"ha_mcp_context": actor},
            )
        raw = await request.read()
        body = json.loads(raw) if raw else {}
        dropped = (
            body.get("method") == "tools/call"
            and body.get("params", {}).get("name") == "native_write"
        )
        # The real buffered adapter consumes the exact bytes once. Close the
        # network after the engine wrote but before aiohttp sends that response.
        response = await self.native.serve_buffered_asgi(
            self.asgi,
            method=request.method,
            path=request.path,
            headers=[(name.lower(), value) for name, value in request.raw_headers],
            body=raw,
            state={**self.lifespan_state, "ha_mcp_context": actor},
        )
        if dropped:
            assert self.completed.is_set()
            if request.transport:
                request.transport.close()
        return response

    async def contracts(self, client: Any) -> None:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert tools["native_read"].input_schema == self.schema
        assert tools["native_read"].annotations.read_only_hint is True
        assert tools["native_write"].annotations.read_only_hint is False
        assert tools["native_write"].annotations.idempotent_hint is False
        result = await client.call_tool("native_read", {"value": "exact"})
        assert result.data == {"value": "exact", "actor": "administrator"}
        assert result.meta["ha_mcp"]["status"] == "confirmed"
        assert result.meta["ha_mcp"]["operation_id"]
        resources = await client.list_resources()
        assert any(str(item.uri) == "skill://test/SKILL.md" for item in resources)
        assert (await client.read_resource("skill://test/SKILL.md"))[
            0
        ].text == "Canonical skill content"
        assert any(item.name == "help_me" for item in await client.list_prompts())
        unknown = await client.call_tool(
            "native_write", {"fail": True}, raise_on_error=False
        )
        assert unknown.is_error
        assert unknown.meta["ha_mcp"]["status"] == "outcome_unknown"
        assert unknown.structured_content["error"]["code"] == "OUTCOME_UNKNOWN"
        # No relist: a live backend tool must bypass the old provider cache.
        self.register(
            ToolDefinition(
                "native_later",
                "Added later",
                self.schema,
                self.read,
                annotations={"readOnlyHint": True},
            )
        )
        assert (await client.call_tool("native_later", {"value": "live"})).data[
            "value"
        ] == "live"

    async def refusal(self, client: Any) -> None:
        await client.list_tools()
        if self.case == "revoked":
            self.valid = False
        try:
            await client.call_tool("native_write", {})
        except (MCPError, ToolError, RuntimeError, TimeoutError):
            pass
        else:
            raise AssertionError("A revoked or lost write was reported as success")
        if self.case == "drop":
            await asyncio.wait_for(self.completed.wait(), 1)
        await self.manager.engine.stop()
        assert self.mutations == (["administrator"] if self.case == "drop" else [])


async def run(case: str) -> None:
    probe = Probe(case)
    async with probe.asgi.lifespan(probe.asgi) as state:
        probe.lifespan_state = state or {}
        app = web.Application()
        app.router.add_route("*", "/api/webhook/test", probe.handle)
        async with TestServer(app) as server:
            proxy = create_native_component_proxy(
                str(server.make_url("/api/webhook/test")), auth="test-credential"
            )
            async with Client(proxy, timeout=3) as client:
                if case == "contracts":
                    await probe.contracts(client)
                else:
                    await probe.refusal(client)
