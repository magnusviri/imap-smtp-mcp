"""Deterministic in-memory adapter for contract and service tests."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone

from imap_smtp_mcp.adapters import MailAdapterError, OperationContext
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


class FakeAdapterError(MailAdapterError):
    """A deterministic missing-object error from the fake provider."""


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        return int(base64.b64decode(cursor + padding, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError):
        raise FakeAdapterError("Invalid pagination cursor.") from None


def _page(items: tuple, request: PageRequest) -> Page:
    start = _offset(request.cursor)
    if start < 0 or start > len(items):
        raise FakeAdapterError("Invalid pagination cursor.")
    end = min(start + request.limit, len(items))
    return Page(
        items=items[start:end], next_cursor=_cursor(end) if end < len(items) else None
    )


@dataclass
class FakeMailAdapter:
    """One-account adapter whose observable behavior is fully deterministic."""

    account: AccountSummary
    mailboxes: tuple[Mailbox, ...] = ()
    messages: tuple[Message, ...] = ()
    attachments: dict[tuple[str, str], Attachment] = field(default_factory=dict)
    drafts: dict[str, Draft] = field(default_factory=dict)
    deliveries: list[tuple[str, str]] = field(default_factory=list)

    def _check(self, context: OperationContext) -> None:
        context.raise_if_cancelled()

    def _check_id(self, message_id: MessageId) -> None:
        if message_id.account_id != self.account.id:
            raise FakeAdapterError("Message belongs to another account.")

    async def get_account(self, context: OperationContext) -> AccountSummary:
        self._check(context)
        return self.account

    async def list_mailboxes(
        self, page: PageRequest, context: OperationContext
    ) -> Page[Mailbox]:
        self._check(context)
        return _page(self.mailboxes, page)

    async def search_messages(
        self, query: SearchQuery, page: PageRequest, context: OperationContext
    ) -> Page[MessageSummary]:
        self._check(context)
        needle = query.text.casefold() if query.text else None
        matches = tuple(
            message.summary
            for message in self.messages
            if (
                query.mailbox_id is None
                or message.summary.mailbox_id == query.mailbox_id
            )
            and (not query.labels or set(query.labels) <= set(message.summary.labels))
            and (
                query.sent_after is None or message.summary.sent_at >= query.sent_after
            )
            and (
                query.sent_before is None
                or message.summary.sent_at <= query.sent_before
            )
            and (
                needle is None
                or needle in message.summary.subject.casefold()
                or needle in message.summary.preview.casefold()
            )
        )
        return _page(matches, page)

    async def get_message(
        self, message_id: MessageId, context: OperationContext
    ) -> Message:
        self._check(context)
        self._check_id(message_id)
        for message in self.messages:
            if message.summary.id == message_id:
                return message
        raise FakeAdapterError("Message was not found.")

    async def create_draft(self, draft: Draft, context: OperationContext) -> Draft:
        self._check(context)
        if draft.account_id != self.account.id:
            raise FakeAdapterError("Draft belongs to another account.")
        if draft.id in self.drafts:
            raise FakeAdapterError("Draft already exists.")
        self.drafts[draft.id] = draft
        return draft

    async def get_draft(self, draft_id: str, context: OperationContext) -> Draft:
        self._check(context)
        try:
            return self.drafts[draft_id]
        except KeyError:
            raise FakeAdapterError("Draft was not found.") from None

    async def get_attachment(
        self,
        message_id: MessageId,
        attachment_id: str,
        context: OperationContext,
    ) -> Attachment:
        self._check(context)
        self._check_id(message_id)
        try:
            return self.attachments[(message_id.serialize(), attachment_id)]
        except KeyError:
            raise FakeAdapterError("Attachment was not found.") from None

    async def send(self, draft: Draft, context: OperationContext) -> DeliveryResult:
        return self._deliver("send", draft, context)

    async def reply(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        self._check_id(source)
        return self._deliver("reply", draft, context)

    async def forward(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        self._check_id(source)
        return self._deliver("forward", draft, context)

    def _deliver(
        self, operation: str, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        self._check(context)
        if draft.account_id != self.account.id:
            raise FakeAdapterError("Draft belongs to another account.")
        provider_id = f"fake-delivery-{len(self.deliveries) + 1}"
        self.deliveries.append((operation, draft.id))
        return DeliveryResult(provider_id, datetime(2000, 1, 1, tzinfo=timezone.utc))
