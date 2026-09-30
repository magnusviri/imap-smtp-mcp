from __future__ import annotations

import anyio
from mcp import Client
from personal_mail_mcp.server import SERVER_NAME, create_server


def test_server_initializes_over_in_memory_transport() -> None:
    async def initialize() -> None:
        async with Client(create_server()) as client:
            assert client.server_info is not None
            assert client.server_info.name == SERVER_NAME
            assert client.protocol_version is not None

    anyio.run(initialize)
