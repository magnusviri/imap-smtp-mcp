from __future__ import annotations

import json

import anyio
from mcp import Client
from personal_mail_mcp.manifest import check_manifest, write_manifest
from personal_mail_mcp.server import SERVER_INSTRUCTIONS, SERVER_NAME, create_server

EXPECTED_TOOLS = {
    "list_accounts",
    "list_mailboxes",
    "search_messages",
    "get_message",
    "create_draft",
    "send_message",
    "reply_to_message",
    "forward_message",
    "get_attachment",
}
READ_TOOLS = {
    "list_accounts",
    "list_mailboxes",
    "search_messages",
    "get_message",
    "get_attachment",
}


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "list_accounts":
            return {
                "accounts": [
                    {
                        "id": "personal",
                        "display_name": "Personal",
                        "sender_addresses": ["owner@example.com"],
                    }
                ]
            }
        raise AssertionError(f"unexpected tool execution: {tool_name}")


def test_server_initializes_over_in_memory_transport() -> None:
    async def initialize() -> None:
        async with Client(create_server()) as client:
            assert client.server_info is not None
            assert client.server_info.name == SERVER_NAME
            assert client.protocol_version is not None
            assert client.instructions == SERVER_INSTRUCTIONS

    anyio.run(initialize)


def test_live_catalog_has_unique_complete_schemas_and_annotations() -> None:
    async def inspect_catalog() -> None:
        async with Client(create_server()) as client:
            result = await client.list_tools()

        names = [tool.name for tool in result.tools]
        assert set(names) == EXPECTED_TOOLS
        assert len(names) == len(set(names))
        for tool in result.tools:
            assert tool.description
            assert tool.input_schema.get("type") == "object"
            assert "properties" in tool.input_schema
            assert tool.output_schema is not None
            assert tool.output_schema.get("type") == "object"
            assert "properties" in tool.output_schema
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is (tool.name in READ_TOOLS)
            assert tool.annotations.destructive_hint is False
            if tool.name in READ_TOOLS:
                assert tool.annotations.idempotent_hint is True

        create = next(tool for tool in result.tools if tool.name == "create_draft")
        assert create.annotations is not None
        assert create.annotations.open_world_hint is False
        for name in ("send_message", "reply_to_message", "forward_message"):
            send = next(tool for tool in result.tools if tool.name == name)
            assert send.annotations is not None
            assert send.annotations.idempotent_hint is True
            assert send.annotations.open_world_hint is True

    anyio.run(inspect_catalog)


def test_tool_executes_through_sdk_with_structured_content() -> None:
    executor = RecordingExecutor()

    async def execute() -> None:
        async with Client(create_server(executor)) as client:
            result = await client.call_tool("list_accounts", {})

        assert result.is_error is False
        assert result.structured_content == {
            "accounts": [
                {
                    "id": "personal",
                    "display_name": "Personal",
                    "sender_addresses": ["owner@example.com"],
                }
            ]
        }

    anyio.run(execute)
    assert executor.calls == [("list_accounts", {})]


def test_manifest_is_generated_from_catalog_and_detects_staleness(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"

    write_manifest(manifest)

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["server"] == SERVER_NAME
    assert document["instructions"] == SERVER_INSTRUCTIONS
    assert {tool["name"] for tool in document["tools"]} == EXPECTED_TOOLS
    assert check_manifest(manifest)

    manifest.write_text("{}\n", encoding="utf-8")
    assert not check_manifest(manifest)


def test_checked_in_manifest_is_fresh() -> None:
    assert check_manifest()
