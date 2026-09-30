"""MCP server construction and console entry point."""

from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer

SERVER_NAME = "personal-mail"


def create_server() -> MCPServer:
    """Create an isolated personal-mail MCP server instance."""
    return MCPServer(SERVER_NAME)


def main() -> None:
    """Run the personal-mail server over the MCP stdio transport."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    create_server().run(transport="stdio")
