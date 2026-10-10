from __future__ import annotations

import asyncio
import imaplib
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from threading import Event

import pytest

from imap_smtp_mcp.adapters import (
    AttachmentAdapter,
    DraftStorageAdapter,
    MailReadAdapter,
    OperationCancelledError,
    OperationContext,
    OutboundDeliveryAdapter,
)
from imap_smtp_mcp.config import EndpointSettings, ImapSmtpAccount
from imap_smtp_mcp.domain import (
    AttachmentMetadata,
    BodyVariants,
    Draft,
    ImapMessageId,
    PageRequest,
    Participant,
    SearchQuery,
)
from imap_smtp_mcp.imap_smtp import ImapSmtpAdapter, ImapSmtpError
from imap_smtp_mcp.secrets import SecretReference, SecretValue

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def raw_message(
    uid: int,
    *,
    message_id: str | None = None,
    references: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Sender Person <sender@example.com>"
    message["To"] = "Owner <owner@example.com>"
    message["Cc"] = "Copy <copy@example.com>"
    message["Subject"] = f"Message {uid}"
    message["Date"] = "Wed, 30 Sep 2026 11:00:00 +0000"
    message["Message-ID"] = message_id or f"<message-{uid}@example.com>"
    if references:
        message["References"] = references
    message.set_content(f"plain body {uid}")
    message.add_alternative(f"<p>html body {uid}</p>", subtype="html")
    message.add_attachment(
        b"report-data",
        maintype="application",
        subtype="pdf",
        filename="report.pdf",
    )
    return message.as_bytes(policy=policy.SMTP)


@dataclass
class FakeSecretProvider:
    value: str = "credential-value"
    error: Exception | None = None
    references: list[SecretReference] = field(default_factory=list)

    def resolve(self, reference: SecretReference) -> SecretValue:
        self.references.append(reference)
        if self.error:
            raise self.error
        return SecretValue(self.value)


@dataclass
class FakeImap:
    messages: dict[str, dict[int, bytes]] = field(
        default_factory=lambda: {
            "INBOX": {1: raw_message(1), 2: raw_message(2)},
            "Archive": {8: raw_message(8)},
            "Drafts": {},
        }
    )
    uid_validity: dict[str, int] = field(
        default_factory=lambda: {"INBOX": 101, "Archive": 202, "Drafts": 303}
    )
    selected: str = ""
    calls: list[tuple] = field(default_factory=list)
    appended: list[tuple[str, str, bytes]] = field(default_factory=list)

    def login(self, username: str, password: str):
        self.calls.append(("login", username, password))
        return "OK", []

    def authenticate(self, mechanism: str, authobject):
        self.calls.append(("authenticate", mechanism, authobject(b"")))
        return "OK", []

    def list(self):
        self.calls.append(("list",))
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Archive"',
            b'(\\HasNoChildren) "/" "Drafts"',
            b'(\\HasNoChildren) "/" "Spam"',
        ]

    def select(self, mailbox: str, readonly: bool = False):
        self.selected = mailbox
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages[mailbox])).encode()]

    def response(self, code: str):
        return code, [str(self.uid_validity[self.selected]).encode()]

    def uid(self, command: str, *arguments: str):
        self.calls.append(("uid", command, *arguments))
        if command == "SEARCH":
            values = b" ".join(
                str(value).encode() for value in self.messages[self.selected]
            )
            return "OK", [values]
        uid = int(arguments[0])
        value = self.messages[self.selected].get(uid)
        if value is None:
            return "NO", []
        return "OK", [(b"RFC822", value), b")"]

    def append(self, mailbox: str, flags: str, date_time, message: bytes):
        self.appended.append((mailbox, flags, message))
        return "OK", []

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []


@dataclass
class FakeSmtp:
    calls: list[tuple] = field(default_factory=list)
    sent: list[tuple[EmailMessage, str, list[str]]] = field(default_factory=list)

    def login(self, username: str, password: str):
        self.calls.append(("login", username, password))

    def auth(self, mechanism: str, authobject):
        self.calls.append(("auth", mechanism, authobject()))

    def send_message(self, message, from_addr: str, to_addrs):
        self.sent.append((message, from_addr, list(to_addrs)))

    def quit(self):
        self.calls.append(("quit",))


def account(*, tls: bool = True, auth_mode: str = "password") -> ImapSmtpAccount:
    return ImapSmtpAccount(
        id="imap-smtp",
        type="imap-smtp",
        sender_addresses=("owner@example.com",),
        mailboxes=("INBOX", "Archive", "Drafts"),
        username="owner@example.com",
        secret_ref=SecretReference("mail-password"),
        imap=EndpointSettings("imap.example.com", 993, tls),
        smtp=EndpointSettings("smtp.example.com", 465, tls),
        auth_mode=auth_mode,
    )


def draft(*, attachments=(), subject: str = "A subject") -> Draft:
    return Draft(
        id="draft-1",
        account_id="imap-smtp",
        sender=Participant("owner@example.com", "Owner"),
        to=(Participant("to@example.com"),),
        cc=(Participant("cc@example.com"),),
        bcc=(Participant("bcc@example.com"),),
        reply_to_address=Participant("replies@example.com"),
        subject=subject,
        body=BodyVariants(text="plain outbound", html="<p>html outbound</p>"),
        created_at=NOW,
        attachments=attachments,
    )


@dataclass
class Harness:
    imap: FakeImap = field(default_factory=FakeImap)
    smtp: FakeSmtp = field(default_factory=FakeSmtp)
    secrets: FakeSecretProvider = field(default_factory=FakeSecretProvider)
    imap_connections: list[tuple[str, int, float]] = field(default_factory=list)
    smtp_connections: list[tuple[str, int, float]] = field(default_factory=list)

    def adapter(self, *, configured: ImapSmtpAccount | None = None, loader=None):
        def imap_factory(host, port, timeout):
            self.imap_connections.append((host, port, timeout))
            return self.imap

        def smtp_factory(host, port, timeout):
            self.smtp_connections.append((host, port, timeout))
            return self.smtp

        return ImapSmtpAdapter(
            configured or account(),
            secret_provider=self.secrets,
            imap_factory=imap_factory,
            smtp_factory=smtp_factory,
            attachment_loader=loader,
        )


def test_adapter_implements_shared_contracts() -> None:
    value = Harness().adapter()
    assert isinstance(value, MailReadAdapter)
    assert isinstance(value, DraftStorageAdapter)
    assert isinstance(value, AttachmentAdapter)
    assert isinstance(value, OutboundDeliveryAdapter)


def test_oauth2_uses_xoauth2_for_imap_and_smtp() -> None:
    harness = Harness()
    adapter = harness.adapter(configured=account(auth_mode="oauth2"))

    run(adapter.list_mailboxes(PageRequest(limit=2), OperationContext(7)))
    run(adapter.send(draft(), OperationContext(7)))

    assert harness.imap.calls[0] == (
        "authenticate",
        "XOAUTH2",
        b"user=owner@example.com\x01auth=Bearer credential-value\x01\x01",
    )
    assert harness.smtp.calls[0] == (
        "auth",
        "XOAUTH2",
        "user=owner@example.com\x01auth=Bearer credential-value\x01\x01",
    )


def test_mailboxes_are_configured_scoped_and_paginated_over_tls() -> None:
    harness = Harness()
    first = run(
        harness.adapter().list_mailboxes(PageRequest(limit=2), OperationContext(7))
    )
    second = run(
        harness.adapter().list_mailboxes(
            PageRequest(limit=2, cursor=first.next_cursor), OperationContext(7)
        )
    )

    assert [item.id for item in first.items] == ["INBOX", "Archive"]
    assert [item.id for item in second.items] == ["Drafts"]
    assert harness.imap_connections == [
        ("imap.example.com", 993, 7),
        ("imap.example.com", 993, 7),
    ]
    assert harness.imap.calls[0] == (
        "login",
        "owner@example.com",
        "credential-value",
    )


def test_search_parses_mime_and_uses_uidvalidity_identity() -> None:
    harness = Harness()
    page = run(
        harness.adapter().search_messages(
            SearchQuery(mailbox_id="INBOX", text="body"),
            PageRequest(limit=1),
            OperationContext(5),
        )
    )

    summary = page.items[0]
    assert summary.id == ImapMessageId("imap-smtp", "INBOX", 101, 2)
    assert summary.sender == Participant("sender@example.com", "Sender Person")
    assert summary.recipients == (
        Participant("owner@example.com", "Owner"),
        Participant("copy@example.com", "Copy"),
    )
    assert summary.preview == "plain body 2"
    assert summary.attachments[0].filename == "report.pdf"
    assert page.next_cursor is not None


def test_get_message_and_attachment_parse_full_mime() -> None:
    harness = Harness()
    source = ImapMessageId("imap-smtp", "INBOX", 101, 1)
    value = harness.adapter()

    message = run(value.get_message(source, OperationContext(5)))
    attachment_id = message.summary.attachments[0].id
    attachment = run(value.get_attachment(source, attachment_id, OperationContext(5)))

    assert message.body.text.strip() == "plain body 1"
    assert message.body.html.strip() == "<p>html body 1</p>"
    assert attachment.content == b"report-data"
    assert attachment.metadata.media_type == "application/pdf"


def test_stale_uidvalidity_and_foreign_scope_are_rejected() -> None:
    value = Harness().adapter()
    with pytest.raises(ImapSmtpError, match="UIDVALIDITY changed"):
        run(
            value.get_message(
                ImapMessageId("imap-smtp", "INBOX", 99, 1), OperationContext(5)
            )
        )
    with pytest.raises(ImapSmtpError, match="another configured account"):
        run(
            value.get_message(
                ImapMessageId("other", "INBOX", 101, 1), OperationContext(5)
            )
        )


def test_smtp_builds_plain_html_recipient_and_attachment_mime() -> None:
    metadata = AttachmentMetadata("local-1", "notes.txt", "text/plain", 5)
    inline = AttachmentMetadata(
        "local-2", "logo.png", "image/png", 5, inline=True, content_id="logo"
    )
    harness = Harness()
    result = run(
        harness.adapter(loader=lambda item: b"hello").send(
            draft(attachments=(metadata, inline)), OperationContext(8)
        )
    )

    message, sender, recipients = harness.smtp.sent[0]
    assert result.provider_id == message["Message-ID"]
    assert sender == "owner@example.com"
    assert recipients == ["to@example.com", "cc@example.com", "bcc@example.com"]
    assert message["To"] == "to@example.com"
    assert message["Cc"] == "cc@example.com"
    assert message["Bcc"] == "bcc@example.com"
    assert message["Reply-To"] == "replies@example.com"
    assert message.get_body(("plain",)).get_content() == "plain outbound\n"
    assert message.get_body(("html",)).get_content() == "<p>html outbound</p>\n"
    attachments = list(message.iter_attachments())
    assert attachments[0].get_payload(decode=True) == b"hello"
    inline_part = next(
        part for part in message.walk() if part["Content-ID"] == "<logo>"
    )
    assert inline_part["Content-Disposition"] == 'inline; filename="logo.png"'
    assert harness.smtp_connections == [("smtp.example.com", 465, 8)]


@pytest.mark.parametrize("operation", ["reply", "forward"])
def test_reply_and_forward_preserve_rfc_threading_headers(operation: str) -> None:
    harness = Harness()
    harness.imap.messages["INBOX"][1] = raw_message(
        1,
        message_id="<parent@example.com>",
        references="<root@example.com>",
    )
    source = ImapMessageId("imap-smtp", "INBOX", 101, 1)

    run(
        getattr(harness.adapter(), operation)(
            source, draft(subject=""), OperationContext(6)
        )
    )

    message = harness.smtp.sent[0][0]
    assert message["In-Reply-To"] == "<parent@example.com>"
    assert message["References"] == "<root@example.com> <parent@example.com>"
    prefix = "Re: " if operation == "reply" else "Fwd: "
    assert message["Subject"] == f"{prefix}Message 1"


def test_create_draft_appends_rfc_message_to_configured_drafts_mailbox() -> None:
    harness = Harness()
    value = draft()

    assert run(harness.adapter().create_draft(value, OperationContext(5))) == value
    mailbox, flags, raw = harness.imap.appended[0]
    assert mailbox == "Drafts"
    assert flags == "\\Draft"
    assert b"Subject: A subject" in raw


def test_tls_cancellation_and_invalid_cursor_fail_before_connecting() -> None:
    harness = Harness()
    with pytest.raises(ImapSmtpError, match="requires TLS"):
        run(
            harness.adapter(configured=account(tls=False)).get_account(
                OperationContext(5)
            )
        )
    cancellation = Event()
    cancellation.set()
    with pytest.raises(OperationCancelledError, match="cancelled"):
        run(harness.adapter().get_account(OperationContext(5, cancellation)))
    with pytest.raises(ImapSmtpError, match="pagination cursor"):
        run(
            harness.adapter().list_mailboxes(
                PageRequest(cursor="bad!"), OperationContext(5)
            )
        )


def test_certificate_and_secret_failures_are_sanitized() -> None:
    secret = "credential-value and complete private message"
    harness = Harness()
    harness.secrets.error = RuntimeError(secret)
    with pytest.raises(ImapSmtpError) as raised:
        run(harness.adapter().send(draft(), OperationContext(5)))
    assert secret not in str(raised.value)

    def bad_factory(host, port, timeout):
        raise ssl.SSLCertVerificationError(secret)

    value = ImapSmtpAdapter(
        account(), secret_provider=FakeSecretProvider(), imap_factory=bad_factory
    )
    with pytest.raises(ImapSmtpError, match="TLS validation failed") as raised:
        run(value.list_mailboxes(PageRequest(), OperationContext(5)))
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (TimeoutError("private provider host"), "connection timed out"),
        (imaplib.IMAP4.error("private server detail"), "authentication failed"),
    ],
)
def test_imap_network_and_authentication_failures_are_actionable_and_sanitized(
    failure: Exception, message: str
) -> None:
    def failing_factory(host, port, timeout):
        raise failure

    value = ImapSmtpAdapter(
        account(), secret_provider=FakeSecretProvider(), imap_factory=failing_factory
    )

    with pytest.raises(ImapSmtpError, match=message) as raised:
        run(value.list_mailboxes(PageRequest(), OperationContext(5)))

    assert str(failure) not in str(raised.value)


def test_smtp_authentication_failure_is_sanitized() -> None:
    class RejectingSmtp(FakeSmtp):
        def login(self, username: str, password: str):
            raise smtplib.SMTPAuthenticationError(535, b"credential-value")

    value = ImapSmtpAdapter(
        account(),
        secret_provider=FakeSecretProvider(),
        smtp_factory=lambda host, port, timeout: RejectingSmtp(),
    )

    with pytest.raises(ImapSmtpError, match="authentication failed") as raised:
        run(value.send(draft(), OperationContext(5)))

    assert "credential-value" not in str(raised.value)
