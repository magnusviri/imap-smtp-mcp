from __future__ import annotations

from datetime import datetime, timezone

import anyio
from mcp import Client

from personal_mail_mcp.domain import (
    AccountSummary,
    Attachment,
    AttachmentMetadata,
    BodyVariants,
    ImapMessageId,
    Mailbox,
    Message,
    MessageSummary,
    Participant,
)
from personal_mail_mcp.fake_adapter import FakeAdapterError, FakeMailAdapter
from personal_mail_mcp.read_service import MAX_PREVIEW_CHARS, ReadService
from personal_mail_mcp.server import create_server

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _message(
    account_id: str = "personal", mailbox_id: str = "inbox", uid: int = 1
) -> Message:
    identifier = ImapMessageId(account_id, mailbox_id, 77, uid)
    metadata = AttachmentMetadata("note", "note.txt", "text/plain", 4)
    return Message(
        summary=MessageSummary(
            id=identifier,
            account_id=account_id,
            mailbox_id=mailbox_id,
            subject="Status",
            sender=Participant("sender@example.com", "Sender"),
            recipients=(Participant("owner@example.com"),),
            sent_at=NOW,
            received_at=NOW,
            preview="p" * (MAX_PREVIEW_CHARS + 50),
            labels=("inbox",),
            rfc_message_id="<status@example.com>",
            attachments=(metadata,),
        ),
        body=BodyVariants(text="complete private body", html="<p>private body</p>"),
    )


def _adapter(account_id: str = "personal") -> FakeMailAdapter:
    message = _message(account_id)
    second_message = _message(account_id, uid=2)
    return FakeMailAdapter(
        account=AccountSummary(
            account_id, account_id.title(), (f"{account_id}@example.com",)
        ),
        mailboxes=(
            Mailbox("inbox", account_id, "Inbox", ("inbox",)),
            Mailbox("archive", account_id, "Archive", ("archive",)),
        ),
        messages=(message, second_message),
        attachments={
            (message.summary.id.serialize(), "note"): Attachment(
                message.summary.attachments[0], b"data"
            )
        },
    )


def _error_text(result) -> str:
    assert result.is_error is True
    return " ".join(getattr(item, "text", "") for item in result.content)


def test_read_tools_over_in_memory_mcp_are_scoped_paginated_and_bounded() -> None:
    personal = _adapter()
    work = _adapter("work")
    service = ReadService(
        {"personal": personal, "work": work},
        attachment_adapters={"personal": personal, "work": work},
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            accounts = await client.call_tool("list_accounts", {})
            assert accounts.is_error is False
            assert [item["id"] for item in accounts.structured_content["accounts"]] == [
                "personal",
                "work",
            ]

            first = await client.call_tool(
                "list_mailboxes", {"account_id": "personal", "limit": 1}
            )
            assert [item["id"] for item in first.structured_content["mailboxes"]] == [
                "inbox"
            ]
            second = await client.call_tool(
                "list_mailboxes",
                {
                    "account_id": "personal",
                    "limit": 1,
                    "cursor": first.structured_content["next_cursor"],
                },
            )
            assert [item["id"] for item in second.structured_content["mailboxes"]] == [
                "archive"
            ]

            found = await client.call_tool(
                "search_messages",
                {
                    "account_id": "personal",
                    "mailbox_id": "inbox",
                    "limit": 1,
                },
            )
            summary = found.structured_content["messages"][0]
            assert len(summary["preview"]) == MAX_PREVIEW_CHARS
            assert "text_body" not in summary
            assert "complete private body" not in str(found.structured_content)
            next_messages = await client.call_tool(
                "search_messages",
                {
                    "account_id": "personal",
                    "mailbox_id": "inbox",
                    "limit": 1,
                    "cursor": found.structured_content["next_cursor"],
                },
            )
            assert len(next_messages.structured_content["messages"]) == 1
            assert next_messages.structured_content["next_cursor"] is None

            full = await client.call_tool(
                "get_message",
                {
                    "account_id": "personal",
                    "message_id": personal.messages[0].summary.id.serialize(),
                },
            )
            assert full.structured_content["text_body"] == "complete private body"

            attachment = await client.call_tool(
                "get_attachment",
                {
                    "account_id": "personal",
                    "message_id": personal.messages[0].summary.id.serialize(),
                    "attachment_id": "note",
                },
            )
            assert attachment.structured_content["content_base64"] == "ZGF0YQ=="
            assert attachment.structured_content["download_path"] is None

    anyio.run(exercise)


def test_read_tools_reject_unknown_and_cross_account_references() -> None:
    personal = _adapter()
    work = _adapter("work")
    service = ReadService({"personal": personal, "work": work})

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            unknown_account = await client.call_tool(
                "list_mailboxes", {"account_id": "missing"}
            )
            assert "Unknown account ID" in _error_text(unknown_account)

            unknown_mailbox = await client.call_tool(
                "search_messages",
                {"account_id": "personal", "mailbox_id": "missing"},
            )
            assert "Unknown mailbox ID" in _error_text(unknown_mailbox)

            cross_account = await client.call_tool(
                "get_message",
                {
                    "account_id": "personal",
                    "message_id": work.messages[0].summary.id.serialize(),
                },
            )
            assert "belongs to another account" in _error_text(cross_account)

            malformed = await client.call_tool(
                "get_message",
                {"account_id": "personal", "message_id": "not-a-message-id"},
            )
            assert "Malformed message identifier" in _error_text(malformed)

    anyio.run(exercise)


def test_attachment_limit_is_enforced_without_a_download_write() -> None:
    adapter = _adapter()
    service = ReadService(
        {"personal": adapter},
        attachment_adapters={"personal": adapter},
        max_inline_attachment_bytes=3,
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "get_attachment",
                {
                    "account_id": "personal",
                    "message_id": adapter.messages[0].summary.id.serialize(),
                    "attachment_id": "note",
                },
            )
        assert "too large" in _error_text(result)
        assert "maximum 3 bytes" in _error_text(result)

    anyio.run(exercise)


class InvalidAttachmentAdapter(FakeMailAdapter):
    async def get_attachment(self, message_id, attachment_id, context):
        del message_id, attachment_id, context
        return Attachment(
            AttachmentMetadata("note", "../private.txt", "not a mime type", 4),
            b"data",
        )


def test_inbound_attachment_mime_metadata_is_validated() -> None:
    source = _adapter()
    adapter = InvalidAttachmentAdapter(
        account=source.account,
        mailboxes=source.mailboxes,
        messages=source.messages,
    )
    service = ReadService(
        {"personal": adapter}, attachment_adapters={"personal": adapter}
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "get_attachment",
                {
                    "account_id": "personal",
                    "message_id": adapter.messages[0].summary.id.serialize(),
                    "attachment_id": "note",
                },
            )
        assert "invalid MIME metadata" in _error_text(result)

    anyio.run(exercise)


class FailingAdapter(FakeMailAdapter):
    async def get_message(self, message_id, context):
        del message_id, context
        raise FakeAdapterError("The requested message is temporarily inaccessible.")


def test_sanitized_adapter_errors_are_preserved_for_mcp_clients() -> None:
    source = _adapter()
    adapter = FailingAdapter(
        account=source.account,
        mailboxes=source.mailboxes,
        messages=source.messages,
    )
    service = ReadService({"personal": adapter})

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "get_message",
                {
                    "account_id": "personal",
                    "message_id": source.messages[0].summary.id.serialize(),
                },
            )
        assert "temporarily inaccessible" in _error_text(result)

    anyio.run(exercise)
