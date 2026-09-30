from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from threading import Event

import pytest
from personal_mail_mcp.adapters import (
    AttachmentAdapter,
    DraftStorageAdapter,
    MailReadAdapter,
    OperationCancelledError,
    OperationContext,
    OutboundDeliveryAdapter,
)
from personal_mail_mcp.domain import (
    AccountSummary,
    Attachment,
    AttachmentMetadata,
    BodyVariants,
    DomainValidationError,
    Draft,
    ImapMessageId,
    Mailbox,
    Message,
    MessageSummary,
    PageRequest,
    Participant,
    ProviderMessageId,
    SearchQuery,
    ThreadReference,
    parse_message_id,
)
from personal_mail_mcp.fake_adapter import FakeAdapterError, FakeMailAdapter

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def make_message(
    uid: int, subject: str, *, labels: tuple[str, ...] = ("inbox",)
) -> Message:
    message_id = ImapMessageId("personal", "inbox", 9001, uid)
    attachment = AttachmentMetadata("report", "report.txt", "text/plain", 4)
    return Message(
        summary=MessageSummary(
            id=message_id,
            account_id="personal",
            mailbox_id="inbox",
            subject=subject,
            sender=Participant("sender@example.com", "Sender"),
            recipients=(Participant("owner@example.com"),),
            sent_at=NOW,
            received_at=NOW,
            preview=f"Preview for {subject}",
            labels=labels,
            rfc_message_id=f"<message-{uid}@example.com>",
            thread=ThreadReference(
                "thread-1",
                in_reply_to="<parent@example.com>",
                references=("<root@example.com>", "<parent@example.com>"),
            ),
            attachments=(attachment,),
        ),
        body=BodyVariants(text=f"Full text for {subject}", html=f"<p>{subject}</p>"),
    )


def make_draft(draft_id: str = "draft-1") -> Draft:
    return Draft(
        id=draft_id,
        account_id="personal",
        sender=Participant("owner@example.com", "Owner"),
        to=(Participant("recipient@example.com"),),
        cc=(),
        bcc=(),
        subject="A draft",
        body=BodyVariants(text="Draft body"),
        created_at=NOW,
        reply_to=ImapMessageId("personal", "inbox", 9001, 1),
    )


def make_adapter() -> FakeMailAdapter:
    messages = (
        make_message(1, "Alpha"),
        make_message(2, "Beta"),
        make_message(3, "Gamma"),
    )
    attachment = Attachment(
        metadata=messages[0].summary.attachments[0], content=b"data"
    )
    return FakeMailAdapter(
        account=AccountSummary("personal", "Personal", ("owner@example.com",)),
        mailboxes=(
            Mailbox("inbox", "personal", "Inbox", ("inbox",)),
            Mailbox("archive", "personal", "Archive", ("archive",)),
        ),
        messages=messages,
        attachments={(messages[0].summary.id.serialize(), "report"): attachment},
    )


@pytest.mark.parametrize(
    "identifier",
    [
        ProviderMessageId("apple", "Inbox", "provider:item/42"),
        ImapMessageId("personal", "INBOX/Receipts", 4294967295, 77),
    ],
)
def test_message_identifier_round_trip(identifier) -> None:
    assert parse_message_id(identifier.serialize()) == identifier


def test_imap_uidvalidity_is_part_of_identity() -> None:
    old = ImapMessageId("personal", "inbox", 10, 4)
    reused = ImapMessageId("personal", "inbox", 11, 4)

    assert old != reused
    assert old.serialize() != reused.serialize()


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: Participant("not-an-address"), "address is invalid"),
        (lambda: BodyVariants(), "body variant"),
        (lambda: ImapMessageId("account", "inbox", 0, 1), "uid_validity"),
        (
            lambda: MessageSummary(
                id=ImapMessageId("other", "inbox", 1, 1),
                account_id="personal",
                mailbox_id="inbox",
                subject="",
                sender=Participant("sender@example.com"),
                recipients=(),
                sent_at=NOW,
                received_at=None,
                preview="",
            ),
            "identifier scope",
        ),
        (
            lambda: MessageSummary(
                id=ImapMessageId("personal", "inbox", 1, 1),
                account_id="personal",
                mailbox_id="inbox",
                subject="",
                sender=Participant("sender@example.com"),
                recipients=(),
                sent_at=datetime(2026, 1, 1),  # noqa: DTZ001 - validation target
                received_at=None,
                preview="",
            ),
            "timezone",
        ),
        (lambda: PageRequest(limit=101), "limit"),
    ],
)
def test_model_validation(factory, message: str) -> None:
    with pytest.raises(DomainValidationError, match=message):
        factory()


@pytest.mark.parametrize(
    "value",
    ["", "personal-mail:imap:nope", "personal-mail:other:e30"],
)
def test_malformed_message_identifiers_are_rejected(value: str) -> None:
    with pytest.raises(DomainValidationError, match="Malformed"):
        parse_message_id(value)


def test_fake_implements_each_adapter_contract() -> None:
    adapter = make_adapter()

    assert isinstance(adapter, MailReadAdapter)
    assert isinstance(adapter, DraftStorageAdapter)
    assert isinstance(adapter, AttachmentAdapter)
    assert isinstance(adapter, OutboundDeliveryAdapter)


def test_fake_read_and_attachment_contract() -> None:
    adapter = make_adapter()
    context = OperationContext(5)

    assert run(adapter.get_account(context)).id == "personal"
    assert [
        item.id for item in run(adapter.list_mailboxes(PageRequest(), context)).items
    ] == [
        "inbox",
        "archive",
    ]
    found = run(
        adapter.search_messages(
            SearchQuery(text="beta", mailbox_id="inbox", labels=("inbox",)),
            PageRequest(),
            context,
        )
    )
    assert [message.subject for message in found.items] == ["Beta"]
    message = run(adapter.get_message(adapter.messages[0].summary.id, context))
    assert message.body.html == "<p>Alpha</p>"
    attachment = run(
        adapter.get_attachment(adapter.messages[0].summary.id, "report", context)
    )
    assert attachment.content == b"data"


def test_fake_pagination_is_stable_and_exhaustive() -> None:
    adapter = make_adapter()
    context = OperationContext(5)
    query = SearchQuery()

    first = run(adapter.search_messages(query, PageRequest(limit=2), context))
    second = run(
        adapter.search_messages(
            query, PageRequest(limit=2, cursor=first.next_cursor), context
        )
    )

    assert [message.subject for message in first.items] == ["Alpha", "Beta"]
    assert first.next_cursor is not None
    assert [message.subject for message in second.items] == ["Gamma"]
    assert second.next_cursor is None


def test_fake_draft_and_delivery_contracts() -> None:
    adapter = make_adapter()
    context = OperationContext(5)
    draft = make_draft()

    assert run(adapter.create_draft(draft, context)) == draft
    assert run(adapter.get_draft(draft.id, context)) == draft
    sent = run(adapter.send(draft, context))
    replied = run(adapter.reply(draft.reply_to, draft, context))
    forwarded = run(adapter.forward(draft.reply_to, draft, context))

    assert [sent.provider_id, replied.provider_id, forwarded.provider_id] == [
        "fake-delivery-1",
        "fake-delivery-2",
        "fake-delivery-3",
    ]
    assert adapter.deliveries == [
        ("send", "draft-1"),
        ("reply", "draft-1"),
        ("forward", "draft-1"),
    ]


def test_fake_honors_cancellation_context() -> None:
    cancellation = Event()
    cancellation.set()

    with pytest.raises(OperationCancelledError, match="cancelled"):
        run(make_adapter().get_account(OperationContext(5, cancellation)))


def test_fake_rejects_invalid_cursor() -> None:
    with pytest.raises(FakeAdapterError, match="cursor"):
        run(
            make_adapter().list_mailboxes(
                PageRequest(cursor="not-valid!"), OperationContext(5)
            )
        )
