"""Asynchronous provider adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from imap_smtp_mcp.domain import (
    AccountSummary,
    Attachment,
    DeliveryResult,
    Draft,
    Mailbox,
    Message,
    MessageId,
    MessageSummary,
    Page,
    PageRequest,
    SearchQuery,
)


class MailAdapterError(RuntimeError):
    """Safe provider-boundary failure suitable for an MCP client."""


class OperationCancelledError(MailAdapterError):
    """Raised before provider work when its caller has cancelled it."""


@runtime_checkable
class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class OperationContext:
    """Per-call cancellation and timeout budget supplied to every adapter."""

    timeout_seconds: float
    cancellation: CancellationSignal | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive.")

    def raise_if_cancelled(self) -> None:
        if self.cancellation is not None and self.cancellation.is_set():
            raise OperationCancelledError("Mail operation was cancelled.")


@runtime_checkable
class MailReadAdapter(Protocol):
    async def get_account(self, context: OperationContext) -> AccountSummary: ...

    async def list_mailboxes(
        self, page: PageRequest, context: OperationContext
    ) -> Page[Mailbox]: ...

    async def search_messages(
        self, query: SearchQuery, page: PageRequest, context: OperationContext
    ) -> Page[MessageSummary]: ...

    async def get_message(
        self, message_id: MessageId, context: OperationContext
    ) -> Message: ...


@runtime_checkable
class DraftStorageAdapter(Protocol):
    async def create_draft(self, draft: Draft, context: OperationContext) -> Draft: ...

    async def get_draft(self, draft_id: str, context: OperationContext) -> Draft: ...


@runtime_checkable
class AttachmentAdapter(Protocol):
    async def get_attachment(
        self,
        message_id: MessageId,
        attachment_id: str,
        context: OperationContext,
    ) -> Attachment: ...


@runtime_checkable
class OutboundDeliveryAdapter(Protocol):
    async def send(self, draft: Draft, context: OperationContext) -> DeliveryResult: ...

    async def reply(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult: ...

    async def forward(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult: ...
