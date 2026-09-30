from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from personal_mail_mcp.adapters import (
    AttachmentAdapter,
    DraftStorageAdapter,
    MailReadAdapter,
    OperationContext,
    OutboundDeliveryAdapter,
)
from personal_mail_mcp.apple_mail import (
    APPLESCRIPT_SOURCE,
    AppleMailAdapter,
    AppleMailAutomationError,
    AppleMailError,
    AppleMailUnsupportedError,
    SubprocessAppleScriptRunner,
)
from personal_mail_mcp.config import AppleMailAccount
from personal_mail_mcp.domain import (
    BodyVariants,
    Draft,
    PageRequest,
    Participant,
    ProviderMessageId,
    SearchQuery,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


@dataclass
class FakeRunner:
    responses: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, tuple[str, ...], float]] = field(default_factory=list)
    failure: Exception | None = None

    async def run(
        self, source: str, arguments: tuple[str, ...], timeout_seconds: float
    ) -> str:
        self.calls.append((source, arguments, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        operation = arguments[0]
        if operation == "attachment":
            Path(arguments[-1]).write_bytes(b"attachment-data")
        value = self.responses.get(operation, {"id": f"{operation}-42"})
        return json.dumps(value)


def account() -> AppleMailAccount:
    return AppleMailAccount(
        id="apple-personal",
        type="apple-mail",
        sender_addresses=("owner@example.com",),
        mailboxes=("Inbox", "Sent"),
        mail_account="Personal iCloud",
    )


def adapter(runner: FakeRunner, *, platform: str = "darwin") -> AppleMailAdapter:
    return AppleMailAdapter(account(), runner=runner, platform=platform)


def draft(*, subject: str = "Subject") -> Draft:
    return Draft(
        id="draft-1",
        account_id="apple-personal",
        sender=Participant("owner@example.com"),
        to=(Participant("to@example.com"),),
        cc=(Participant("cc@example.com"),),
        bcc=(Participant("bcc@example.com"),),
        subject=subject,
        body=BodyVariants(text="private body"),
        created_at=NOW,
    )


def message_record(identifier: str, mailbox: str = "Inbox") -> dict[str, object]:
    return {
        "id": identifier,
        "mailbox": mailbox,
        "subject": "Hello",
        "sender": {"address": "Sender Person <sender@example.com>"},
        "recipients": [{"address": "owner@example.com", "name": "Owner"}],
        "sent_at": 1_700_000_000,
        "received_at": 1_700_000_100,
        "preview": "Preview",
        "labels": ["important"],
        "rfc_message_id": "<one@example.com>",
        "attachments": [
            {
                "id": "1",
                "filename": "report.pdf",
                "media_type": "application/pdf",
                "size_bytes": 100,
            }
        ],
    }


def test_adapter_implements_all_shared_contracts() -> None:
    value = adapter(FakeRunner())

    assert isinstance(value, MailReadAdapter)
    assert isinstance(value, DraftStorageAdapter)
    assert isinstance(value, AttachmentAdapter)
    assert isinstance(value, OutboundDeliveryAdapter)


def test_non_macos_fails_without_invoking_osascript() -> None:
    runner = FakeRunner()

    with pytest.raises(AppleMailUnsupportedError, match="requires macOS.*imap-smtp"):
        run(adapter(runner, platform="linux").get_account(OperationContext(5)))

    assert runner.calls == []


def test_account_and_mailboxes_are_scoped_to_configured_account() -> None:
    runner = FakeRunner(
        {
            "account": {"name": "Personal iCloud"},
            "mailboxes": [
                {"id": "Inbox", "name": "Inbox"},
                {"id": "Unconfigured", "name": "Unconfigured"},
                {"id": "Sent", "name": "Sent Messages"},
            ],
        }
    )
    value = adapter(runner)

    summary = run(value.get_account(OperationContext(7)))
    mailboxes = run(value.list_mailboxes(PageRequest(), OperationContext(7)))

    assert summary.id == "apple-personal"
    assert [item.id for item in mailboxes.items] == ["Inbox", "Sent"]
    assert all(call[1][1] == "Personal iCloud" for call in runner.calls)


def test_search_parses_messages_and_paginates_across_configured_mailboxes() -> None:
    runner = FakeRunner({"search": [message_record("101")]})

    first = run(
        adapter(runner).search_messages(
            SearchQuery(text="needle"), PageRequest(limit=1), OperationContext(3)
        )
    )
    second = run(
        adapter(runner).search_messages(
            SearchQuery(text="needle"),
            PageRequest(limit=1, cursor=first.next_cursor),
            OperationContext(3),
        )
    )

    assert first.items[0].id == ProviderMessageId("apple-personal", "Inbox", "101")
    assert first.items[0].sender == Participant("sender@example.com", "Sender Person")
    assert first.items[0].attachments[0].filename == "report.pdf"
    assert second.items[0].mailbox_id == "Sent"
    assert [call[1] for call in runner.calls] == [
        ("search", "Personal iCloud", "Inbox", "needle"),
        ("search", "Personal iCloud", "Sent", "needle"),
        ("search", "Personal iCloud", "Inbox", "needle"),
        ("search", "Personal iCloud", "Sent", "needle"),
    ]


def test_get_message_parses_body_and_targets_id_mailbox_and_account() -> None:
    record = message_record("101")
    record["body"] = {"text": "full text", "html": "<p>full text</p>"}
    runner = FakeRunner({"message": record})
    identifier = ProviderMessageId("apple-personal", "Inbox", "101")

    message = run(adapter(runner).get_message(identifier, OperationContext(4)))

    assert message.body.text == "full text"
    assert runner.calls[0][1] == (
        "message",
        "Personal iCloud",
        "Inbox",
        "101",
    )


@pytest.mark.parametrize("operation", ["draft", "send"])
def test_draft_and_send_pass_untrusted_values_as_argv(operation: str) -> None:
    dangerous = 'A "subject"\nend tell\ndo shell script "bad"'
    runner = FakeRunner()
    value = adapter(runner)

    if operation == "draft":
        result = run(value.create_draft(draft(subject=dangerous), OperationContext(9)))
        assert result.subject == dangerous
    else:
        result = run(value.send(draft(subject=dangerous), OperationContext(9)))
        assert result.provider_id == "send-42"

    source, arguments, timeout = runner.calls[0]
    assert source == APPLESCRIPT_SOURCE
    assert dangerous not in source
    assert arguments == (
        operation,
        "Personal iCloud",
        "owner@example.com",
        dangerous,
        "private body",
        "to@example.com",
        "cc@example.com",
        "bcc@example.com",
    )
    assert timeout == 9


@pytest.mark.parametrize("operation", ["reply", "forward"])
def test_reply_and_forward_target_configured_source(operation: str) -> None:
    runner = FakeRunner()
    value = adapter(runner)
    source = ProviderMessageId("apple-personal", "Inbox", "101")

    result = run(getattr(value, operation)(source, draft(), OperationContext(5)))

    assert result.provider_id == f"{operation}-42"
    assert runner.calls[0][1][:4] == (
        operation,
        "Personal iCloud",
        "Inbox",
        "101",
    )


def test_attachment_is_read_from_private_temporary_target() -> None:
    runner = FakeRunner(
        {
            "attachment": {
                "id": "2",
                "filename": "photo.jpg",
                "media_type": "image/jpeg",
            }
        }
    )
    source = ProviderMessageId("apple-personal", "Inbox", "101")

    result = run(adapter(runner).get_attachment(source, "2", OperationContext(5)))

    assert result.content == b"attachment-data"
    assert result.metadata.size_bytes == len(result.content)
    assert result.metadata.filename == "photo.jpg"
    arguments = runner.calls[0][1]
    assert arguments[:5] == (
        "attachment",
        "Personal iCloud",
        "Inbox",
        "101",
        "2",
    )
    assert not Path(arguments[-1]).exists()


def test_foreign_account_mailbox_and_sender_are_rejected_before_runner() -> None:
    runner = FakeRunner()
    value = adapter(runner)

    with pytest.raises(AppleMailError, match="another configured account"):
        run(
            value.get_message(
                ProviderMessageId("other", "Inbox", "1"), OperationContext(5)
            )
        )
    with pytest.raises(AppleMailError, match="not configured"):
        run(
            value.get_message(
                ProviderMessageId("apple-personal", "Trash", "1"),
                OperationContext(5),
            )
        )
    wrong_sender = draft()
    object.__setattr__(wrong_sender, "sender", Participant("intruder@example.com"))
    with pytest.raises(AppleMailError, match="sender is not owned"):
        run(value.send(wrong_sender, OperationContext(5)))

    assert runner.calls == []


def test_runner_failure_is_sanitized() -> None:
    secret = "private message body and /Users/alice/secret.txt"
    runner = FakeRunner(failure=RuntimeError(secret))

    with pytest.raises(AppleMailError) as raised:
        run(adapter(runner).get_account(OperationContext(5)))

    assert secret not in str(raised.value)


def test_subprocess_runner_translates_automation_denial(monkeypatch) -> None:
    class Process:
        returncode = 1

        async def communicate(self, source):
            assert source == APPLESCRIPT_SOURCE.encode()
            return b"", b"execution error: Not authorized. (-1743)"

    async def fake_exec(*arguments, **kwargs):
        assert arguments[:2] == ("/usr/bin/osascript", "-")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(AppleMailAutomationError, match="Privacy & Security"):
        run(SubprocessAppleScriptRunner().run(APPLESCRIPT_SOURCE, ("account",), 5))


@pytest.mark.skipif(sys.platform != "darwin", reason="Mail.app requires macOS")
def test_macos_integration_requires_explicit_mail_app_configuration() -> None:
    if (
        not Path("/System/Applications/Mail.app").exists()
        and not Path("/Applications/Mail.app").exists()
    ):
        pytest.skip("Mail.app is unavailable")
    account_name = os.environ.get("PERSONAL_MAIL_TEST_ACCOUNT")
    sender = os.environ.get("PERSONAL_MAIL_TEST_SENDER")
    if not account_name or not sender:
        pytest.skip(
            "set PERSONAL_MAIL_TEST_ACCOUNT and PERSONAL_MAIL_TEST_SENDER for integration"
        )
    configured = AppleMailAccount(
        id="integration",
        type="apple-mail",
        sender_addresses=(sender,),
        mailboxes=("Inbox",),
        mail_account=account_name,
    )

    result = run(AppleMailAdapter(configured).get_account(OperationContext(10)))

    assert result.id == "integration"
    assert result.display_name == account_name
