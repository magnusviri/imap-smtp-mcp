from __future__ import annotations

import anyio
import pytest
from personal_mail_mcp.runtime import ConfiguredExecutor, default_state_directory


class RecordingService:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = []

    async def execute(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return {"service": self.name}


def test_configured_executor_dispatches_read_and_outbound_tools() -> None:
    read = RecordingService("read")
    outbound = RecordingService("outbound")
    executor = ConfiguredExecutor(read, outbound)

    async def exercise() -> None:
        assert await executor.execute("list_accounts", {}) == {"service": "read"}
        assert await executor.execute("create_draft", {"account_id": "mail"}) == {
            "service": "outbound"
        }

    anyio.run(exercise)
    assert read.calls == [("list_accounts", {})]
    assert outbound.calls == [("create_draft", {"account_id": "mail"})]


def test_state_directory_honors_explicit_and_xdg_locations() -> None:
    assert (
        default_state_directory(
            {"PERSONAL_MAIL_MCP_STATE_DIR": "/private/mail-state"}
        ).as_posix()
        == "/private/mail-state"
    )
    assert (
        default_state_directory({"XDG_STATE_HOME": "/private/state"}).as_posix()
        == "/private/state/personal-mail-mcp"
    )


@pytest.mark.parametrize(
    "environment",
    [
        {"PERSONAL_MAIL_MCP_STATE_DIR": "relative/state"},
        {"XDG_STATE_HOME": "relative/state"},
    ],
)
def test_state_directory_rejects_relative_overrides(environment) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        default_state_directory(environment)
