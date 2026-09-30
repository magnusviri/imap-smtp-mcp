"""Compose configured provider adapters into the MCP tool executor."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from personal_mail_mcp.apple_mail import AppleMailAdapter
from personal_mail_mcp.config import AppleMailAccount, PersonalMailConfig
from personal_mail_mcp.imap_smtp import ImapSmtpAdapter
from personal_mail_mcp.outbound_service import (
    AuditLog,
    GuardedOutboundService,
    OutboundPolicy,
    SqliteDraftStore,
)
from personal_mail_mcp.read_service import ReadService
from personal_mail_mcp.secrets import FileSecretProvider

READ_TOOLS = {
    "list_accounts",
    "list_mailboxes",
    "search_messages",
    "get_message",
    "get_attachment",
}


class _Executor(Protocol):
    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...


class ConfiguredExecutor:
    """Dispatch the public catalog across configured read and outbound services."""

    def __init__(self, read: _Executor, outbound: _Executor) -> None:
        self._read = read
        self._outbound = outbound

    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        service = self._read if tool_name in READ_TOOLS else self._outbound
        return await service.execute(tool_name, arguments)


def create_configured_executor(
    config: PersonalMailConfig,
    *,
    state_directory: str | Path | None = None,
) -> ConfiguredExecutor:
    """Build all provider adapters and durable local state from validated config."""
    adapters = {}
    secrets_directory = os.environ.get("PERSONAL_MAIL_MCP_SECRETS_DIR")
    secret_provider = (
        FileSecretProvider(secrets_directory) if secrets_directory else None
    )
    for account in config.accounts:
        if isinstance(account, AppleMailAccount):
            adapter = AppleMailAdapter(account)
        else:
            adapter = ImapSmtpAdapter(
                account,
                secret_provider=secret_provider,
                keychain_service=config.server.keychain_service,
                attachment_loader=lambda metadata: Path(metadata.id).read_bytes(),
            )
        adapters[account.id] = adapter

    state = Path(state_directory) if state_directory else default_state_directory()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    policies = {
        account.id: OutboundPolicy.from_account(account) for account in config.accounts
    }
    read = ReadService(
        adapters,
        timeout_seconds=config.server.request_timeout_seconds,
    )
    outbound = GuardedOutboundService(
        adapters,
        policies,
        draft_store=SqliteDraftStore(state / "drafts.sqlite3"),
        audit_log=AuditLog(state / "audit.jsonl"),
        timeout_seconds=config.server.request_timeout_seconds,
    )
    return ConfiguredExecutor(read, outbound)


def default_state_directory(environment: Mapping[str, str] | None = None) -> Path:
    """Return the per-user local state directory without accepting credentials."""
    selected = os.environ if environment is None else environment
    if value := selected.get("PERSONAL_MAIL_MCP_STATE_DIR"):
        state = Path(value).expanduser()
        if not state.is_absolute():
            raise ValueError("PERSONAL_MAIL_MCP_STATE_DIR must be an absolute path.")
        return state
    if value := selected.get("XDG_STATE_HOME"):
        state_home = Path(value).expanduser()
        if not state_home.is_absolute():
            raise ValueError("XDG_STATE_HOME must be an absolute path.")
        return state_home / "personal-mail-mcp"
    return Path.home() / ".local" / "state" / "personal-mail-mcp"
