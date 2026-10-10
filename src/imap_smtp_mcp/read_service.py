"""Account-scoped application service for read-only MCP mail tools."""

from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Mapping
from datetime import datetime
from typing import Any, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from imap_smtp_mcp.adapters import (
    AttachmentAdapter,
    MailAdapterError,
    MailReadAdapter,
    OperationContext,
)
from imap_smtp_mcp.domain import (
    AttachmentMetadata,
    DomainValidationError,
    Mailbox,
    MessageId,
    MessageSummary,
    PageRequest,
    Participant,
    SearchQuery,
    parse_message_id,
)

MAX_PREVIEW_CHARS = 240
MAX_INLINE_ATTACHMENT_BYTES = 1024 * 1024
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_MAILBOX_SCAN_PAGE_SIZE = 100
_READ_TOOLS = {
    "list_accounts",
    "list_mailboxes",
    "search_messages",
    "get_message",
    "get_attachment",
}
T = TypeVar("T")


class ReadService:
    """Route MCP read operations to explicitly selected account adapters."""

    def __init__(
        self,
        adapters: Mapping[str, MailReadAdapter],
        *,
        attachment_adapters: Mapping[str, AttachmentAdapter] | None = None,
        timeout_seconds: float = 30.0,
        max_inline_attachment_bytes: int = MAX_INLINE_ATTACHMENT_BYTES,
    ) -> None:
        if not adapters:
            raise ValueError("At least one mail adapter is required.")
        if any(not account_id.strip() for account_id in adapters):
            raise ValueError("Adapter account IDs must be non-empty.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if max_inline_attachment_bytes < 0:
            raise ValueError("max_inline_attachment_bytes cannot be negative.")
        self._adapters = dict(adapters)
        self._attachment_adapters = (
            dict(attachment_adapters)
            if attachment_adapters is not None
            else {
                account_id: adapter
                for account_id, adapter in adapters.items()
                if isinstance(adapter, AttachmentAdapter)
            }
        )
        self._timeout_seconds = timeout_seconds
        self._max_inline_attachment_bytes = max_inline_attachment_bytes

    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute one MCP tool using only validated, account-scoped inputs."""
        if tool_name not in _READ_TOOLS:
            raise ToolError(f"Tool {tool_name!r} is not available in the read service.")
        if tool_name == "list_accounts":
            return await self._list_accounts()

        account_id = arguments.get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ToolError("A configured account_id is required.")
        adapter = self._adapter(account_id)
        context = OperationContext(self._timeout_seconds)

        if tool_name == "list_mailboxes":
            result = await self._provider(
                adapter.list_mailboxes(self._page(arguments), context),
                "list mailboxes",
            )
            self._validate_mailboxes(result.items, account_id)
            return {
                "mailboxes": [_mailbox(item) for item in result.items],
                "next_cursor": result.next_cursor,
            }
        if tool_name == "search_messages":
            mailbox_id = arguments.get("mailbox_id")
            if mailbox_id is not None:
                if not isinstance(mailbox_id, str) or not mailbox_id.strip():
                    raise ToolError("mailbox_id must be a non-empty string.")
                await self._require_mailbox(adapter, account_id, mailbox_id, context)
            try:
                query = SearchQuery(
                    text=arguments.get("query"),
                    mailbox_id=mailbox_id,
                    labels=tuple(arguments.get("labels") or ()),
                    sent_after=self._timestamp(
                        arguments.get("sent_after"), "sent_after"
                    ),
                    sent_before=self._timestamp(
                        arguments.get("sent_before"), "sent_before"
                    ),
                )
            except DomainValidationError as error:
                raise ToolError(str(error)) from None
            result = await self._provider(
                adapter.search_messages(query, self._page(arguments), context),
                "search messages",
            )
            self._validate_messages(result.items, account_id)
            return {
                "messages": [_message(item) for item in result.items],
                "next_cursor": result.next_cursor,
            }

        message_id = self._message_id(arguments.get("message_id"), account_id)
        await self._require_mailbox(adapter, account_id, message_id.mailbox_id, context)
        if tool_name == "get_message":
            message = await self._provider(
                adapter.get_message(message_id, context), "read the message"
            )
            if message.summary.id != message_id:
                raise ToolError("The mail provider returned a different message.")
            return {
                "message": _message(message.summary),
                "text_body": message.body.text,
                "html_body": message.body.html,
            }

        attachment_id = arguments.get("attachment_id")
        if not isinstance(attachment_id, str) or not attachment_id.strip():
            raise ToolError("attachment_id must be a non-empty string.")
        attachment_adapter = self._attachment_adapters.get(account_id)
        if attachment_adapter is None:
            raise ToolError(
                f"Account {account_id!r} does not support attachment retrieval."
            )
        attachment = await self._provider(
            attachment_adapter.get_attachment(message_id, attachment_id, context),
            "retrieve the attachment",
        )
        self._validate_attachment(attachment.metadata, attachment_id)
        if attachment.metadata.size_bytes > self._max_inline_attachment_bytes:
            raise ToolError(
                "Attachment is too large to return inline "
                f"(maximum {self._max_inline_attachment_bytes} bytes)."
            )
        return {
            "attachment": _attachment(attachment.metadata),
            "content_base64": base64.b64encode(attachment.content).decode("ascii"),
            "download_path": None,
        }

    @staticmethod
    def _validate_attachment(metadata: AttachmentMetadata, requested_id: str) -> None:
        if metadata.id != requested_id:
            raise ToolError("The mail provider returned a different attachment.")
        if not _MEDIA_TYPE_RE.fullmatch(metadata.media_type):
            raise ToolError("The attachment has invalid MIME metadata.")
        if metadata.filename is not None and (
            "/" in metadata.filename
            or "\\" in metadata.filename
            or any(ord(character) < 32 for character in metadata.filename)
        ):
            raise ToolError("The attachment has invalid MIME metadata.")

    async def _list_accounts(self) -> dict[str, Any]:
        accounts = []
        for configured_id, adapter in self._adapters.items():
            account = await self._provider(
                adapter.get_account(OperationContext(self._timeout_seconds)),
                "read the configured account",
            )
            if account.id != configured_id:
                raise ToolError(
                    f"Configured adapter {configured_id!r} returned a different account ID."
                )
            accounts.append(
                {
                    "id": account.id,
                    "display_name": account.display_name,
                    "sender_addresses": list(account.sender_addresses),
                }
            )
        return {"accounts": accounts}

    def _adapter(self, account_id: str) -> MailReadAdapter:
        try:
            return self._adapters[account_id]
        except KeyError:
            raise ToolError(f"Unknown account ID {account_id!r}.") from None

    @staticmethod
    def _validate_mailboxes(mailboxes: tuple[Mailbox, ...], account_id: str) -> None:
        if any(mailbox.account_id != account_id for mailbox in mailboxes):
            raise ToolError(
                "The mail provider returned a mailbox from another account."
            )

    @staticmethod
    def _validate_messages(
        messages: tuple[MessageSummary, ...], account_id: str
    ) -> None:
        if any(message.account_id != account_id for message in messages):
            raise ToolError(
                "The mail provider returned a message from another account."
            )

    async def _require_mailbox(
        self,
        adapter: MailReadAdapter,
        account_id: str,
        mailbox_id: str,
        context: OperationContext,
    ) -> None:
        cursor = None
        while True:
            page = await self._provider(
                adapter.list_mailboxes(
                    PageRequest(limit=_MAILBOX_SCAN_PAGE_SIZE, cursor=cursor), context
                ),
                "validate the mailbox",
            )
            self._validate_mailboxes(page.items, account_id)
            if any(mailbox.id == mailbox_id for mailbox in page.items):
                return
            if page.next_cursor is None:
                raise ToolError(f"Unknown mailbox ID {mailbox_id!r} for this account.")
            cursor = page.next_cursor

    @staticmethod
    def _page(arguments: Mapping[str, Any]) -> PageRequest:
        try:
            return PageRequest(
                limit=arguments.get("limit", 50), cursor=arguments.get("cursor")
            )
        except DomainValidationError as error:
            raise ToolError(str(error)) from None

    @staticmethod
    def _timestamp(value: Any, field: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ToolError(f"{field} must be an RFC 3339 timestamp.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed
        except ValueError:
            raise ToolError(f"{field} must be an RFC 3339 timestamp.") from None

    @staticmethod
    def _message_id(value: Any, account_id: str) -> MessageId:
        if not isinstance(value, str):
            raise ToolError("Malformed message identifier.")
        try:
            message_id = parse_message_id(value)
        except DomainValidationError as error:
            raise ToolError(str(error)) from None
        if message_id.account_id != account_id:
            raise ToolError("The message reference belongs to another account.")
        return message_id

    @staticmethod
    async def _provider(operation: Awaitable[T], action: str) -> T:
        try:
            return await operation
        except MailAdapterError as error:
            raise ToolError(str(error)) from None
        # Adapters are injected provider boundaries. Replace unexpected failures
        # so provider libraries cannot leak message or credential material.
        except Exception:  # noqa: BLE001
            raise ToolError(f"The mail provider could not {action}.") from None


def _participant(participant: Participant) -> dict[str, Any]:
    return {"address": participant.address, "name": participant.name}


def _attachment(attachment: AttachmentMetadata) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "media_type": attachment.media_type,
        "size_bytes": attachment.size_bytes,
        "inline": attachment.inline,
        "content_id": attachment.content_id,
    }


def _mailbox(mailbox: Mailbox) -> dict[str, Any]:
    return {
        "id": mailbox.id,
        "account_id": mailbox.account_id,
        "display_name": mailbox.display_name,
        "labels": list(mailbox.labels),
    }


def _message(message: MessageSummary) -> dict[str, Any]:
    return {
        "id": message.id.serialize(),
        "account_id": message.account_id,
        "mailbox_id": message.mailbox_id,
        "subject": message.subject,
        "sender": _participant(message.sender),
        "recipients": [_participant(item) for item in message.recipients],
        "sent_at": message.sent_at.isoformat(),
        "received_at": (
            message.received_at.isoformat() if message.received_at is not None else None
        ),
        "preview": message.preview[:MAX_PREVIEW_CHARS],
        "labels": list(message.labels),
        "rfc_message_id": message.rfc_message_id,
        "thread_id": message.thread.thread_id if message.thread is not None else None,
        "attachments": [_attachment(item) for item in message.attachments],
    }
