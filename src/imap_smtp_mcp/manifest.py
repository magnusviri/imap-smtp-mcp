"""Generate and verify the checked-in MCP tool manifest."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from imap_smtp_mcp.server import SERVER_INSTRUCTIONS, SERVER_NAME, create_server

DEFAULT_MANIFEST_PATH = Path(__file__).with_name("manifest.json")


async def build_manifest() -> dict[str, Any]:
    """Build a deterministic manifest from the live runtime catalog."""
    tools = await create_server().list_tools()
    return {
        "server": SERVER_NAME,
        "instructions": SERVER_INSTRUCTIONS,
        "tools": [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in tools
        ],
    }


def render_manifest() -> str:
    """Render the live catalog as stable, reviewable JSON."""
    return json.dumps(asyncio.run(build_manifest()), indent=2, sort_keys=True) + "\n"


def write_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> None:
    """Write a generated manifest."""
    path.write_text(render_manifest(), encoding="utf-8")


def check_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> bool:
    """Return whether a checked-in manifest matches the runtime catalog."""
    try:
        return path.read_text(encoding="utf-8") == render_manifest()
    except OSError:
        return False


def _path(arguments: Sequence[str] | None) -> Path:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST_PATH)
    return parser.parse_args(arguments).output


def generate_main(arguments: Sequence[str] | None = None) -> None:
    """Console command that regenerates the manifest."""
    write_manifest(_path(arguments))


def check_main(arguments: Sequence[str] | None = None) -> None:
    """Console command that exits nonzero when the manifest is stale."""
    path = _path(arguments)
    if not check_manifest(path):
        raise SystemExit(
            f"MCP manifest is stale: run imap-smtp-mcp-generate-manifest "
            f"--output {path}"
        )
