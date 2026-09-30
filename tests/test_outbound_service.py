from __future__ import annotations

import json
import stat
from datetime import datetime, timezone

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from personal_mail_mcp.config import parse_config
from personal_mail_mcp.domain import AccountSummary, ImapMessageId
from personal_mail_mcp.fake_adapter import FakeMailAdapter
from personal_mail_mcp.outbound_service import (
    AuditLog,
    GuardedOutboundService,
    InMemoryDraftStore,
    OutboundPolicy,
    SqliteDraftStore,
)
from personal_mail_mcp.server import create_server

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _content(**changes):
    value = {
        "sender": "owner@example.com",
        "to": [{"address": "friend@example.net", "name": "Friend"}],
        "cc": [],
        "bcc": [],
        "reply_to": None,
        "subject": "Private subject",
        "body": {"text": "private body", "html": None},
        "attachment_paths": [],
    }
    value.update(changes)
    return value


def _service(tmp_path, *, policy=None, clock=None):
    adapter = FakeMailAdapter(
        AccountSummary("personal", "Personal", ("owner@example.com",))
    )
    store = InMemoryDraftStore()
    service = GuardedOutboundService(
        {"personal": adapter},
        {
            "personal": policy
            or OutboundPolicy(
                sender_addresses=("owner@example.com",),
                allowed_recipients=("friend@example.net",),
            )
        },
        draft_store=store,
        audit_log=AuditLog(tmp_path / "audit.jsonl"),
        clock=clock or (lambda: NOW),
        id_factory=iter([f"draft-{index}" for index in range(1, 30)]).__next__,
    )
    return service, adapter, store


def _run(awaitable):
    async def exercise():
        return await awaitable

    return anyio.run(exercise)


def test_draft_lifecycle_and_idempotent_send_through_mcp(tmp_path) -> None:
    service, adapter, store = _service(tmp_path)

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            created = await client.call_tool(
                "create_draft", {"account_id": "personal", "content": _content()}
            )
            assert created.is_error is False
            assert created.structured_content == {
                "draft_id": "draft-1",
                "account_id": "personal",
                "status": "draft",
            }
            arguments = {
                "account_id": "personal",
                "draft_id": "draft-1",
                "confirm_send": True,
                "idempotency_key": "retry-key",
            }
            first = await client.call_tool("send_message", arguments)
            second = await client.call_tool("send_message", arguments)
            assert first.structured_content == second.structured_content

    anyio.run(exercise)
    assert store.get("draft-1") is not None
    assert adapter.deliveries == [("send", "draft-1")]


def test_reply_and_forward_keep_source_reference_and_share_guarded_path(
    tmp_path,
) -> None:
    service, adapter, store = _service(tmp_path)
    source = ImapMessageId("personal", "INBOX", 7, 10).serialize()
    reply = _run(
        service.execute(
            "create_draft",
            {
                "account_id": "personal",
                "content": _content(subject="Re: hello"),
                "reply_to_message_id": source,
                "forward_of_message_id": None,
            },
        )
    )
    assert store.get(reply["draft_id"]).reply_to.serialize() == source
    _run(
        service.execute(
            "send_message",
            {
                "account_id": "personal",
                "draft_id": reply["draft_id"],
                "confirm_send": True,
                "idempotency_key": "reply-draft",
            },
        )
    )
    direct = {
        "account_id": "personal",
        "message_id": source,
        "content": _content(subject="Fwd: hello"),
        "confirm_send": True,
        "idempotency_key": "forward-direct",
    }
    first = _run(service.execute("forward_message", direct))
    second = _run(service.execute("forward_message", direct))
    assert first == second
    assert [operation for operation, _ in adapter.deliveries] == ["reply", "forward"]


@pytest.mark.parametrize(
    ("content", "policy", "message"),
    [
        (
            _content(sender="intruder@example.com"),
            OutboundPolicy(("owner@example.com",), ("friend@example.net",)),
            "sender address",
        ),
        (
            _content(to=[{"address": "outside@example.org"}]),
            OutboundPolicy(("owner@example.com",), ("friend@example.net",)),
            "allowlist",
        ),
    ],
)
def test_policy_denials_never_reach_provider(
    tmp_path, content, policy, message
) -> None:
    service, adapter, _store = _service(tmp_path, policy=policy)
    created = _run(
        service.execute("create_draft", {"account_id": "personal", "content": content})
    )
    with pytest.raises(ToolError, match=message):
        _run(
            service.execute(
                "send_message",
                {
                    "account_id": "personal",
                    "draft_id": created["draft_id"],
                    "confirm_send": True,
                    "idempotency_key": "denied",
                },
            )
        )
    assert adapter.deliveries == []


def test_confirmation_attachment_limit_and_rate_limit_prevent_send(tmp_path) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_bytes(b"1234")
    policy = OutboundPolicy(
        ("owner@example.com",),
        allowed_recipient_domains=("example.net",),
        max_attachment_bytes=3,
        rate_limit_messages=1,
    )
    service, adapter, _store = _service(tmp_path, policy=policy)
    created = _run(
        service.execute(
            "create_draft",
            {
                "account_id": "personal",
                "content": _content(attachment_paths=[str(attachment)]),
            },
        )
    )
    base = {
        "account_id": "personal",
        "draft_id": created["draft_id"],
        "idempotency_key": "attachment",
    }
    with pytest.raises(ToolError, match="confirm_send=true"):
        _run(service.execute("send_message", {**base, "confirm_send": False}))
    with pytest.raises(ToolError, match="combined size limit"):
        _run(service.execute("send_message", {**base, "confirm_send": True}))
    assert adapter.deliveries == []

    plain = _run(
        service.execute(
            "create_draft", {"account_id": "personal", "content": _content()}
        )
    )
    _run(
        service.execute(
            "send_message",
            {
                "account_id": "personal",
                "draft_id": plain["draft_id"],
                "confirm_send": True,
                "idempotency_key": "first",
            },
        )
    )
    with pytest.raises(ToolError, match="rate limit"):
        _run(
            service.execute(
                "send_message",
                {
                    "account_id": "personal",
                    "draft_id": plain["draft_id"],
                    "confirm_send": True,
                    "idempotency_key": "second",
                },
            )
        )
    assert len(adapter.deliveries) == 1


def test_audit_log_is_redacted_and_local_store_round_trips(tmp_path) -> None:
    service, _adapter, store = _service(tmp_path)
    created = _run(
        service.execute(
            "create_draft", {"account_id": "personal", "content": _content()}
        )
    )
    _run(
        service.execute(
            "send_message",
            {
                "account_id": "personal",
                "draft_id": created["draft_id"],
                "confirm_send": True,
                "idempotency_key": "audited",
            },
        )
    )
    text = (tmp_path / "audit.jsonl").read_text()
    record = json.loads(text)
    assert record["action"] == "send_message"
    assert record["account_id"] == "personal"
    assert record["idempotency_key"] == "audited"
    assert len(record["recipient_hashes"][0]) == 64
    assert "friend@example.net" not in text
    assert "Private subject" not in text
    assert "private body" not in text

    sqlite_store = SqliteDraftStore(tmp_path / "state" / "drafts.sqlite3")
    sqlite_store.put(store.get(created["draft_id"]))
    restored = sqlite_store.get(created["draft_id"])
    assert restored == store.get(created["draft_id"])
    assert stat.S_IMODE((tmp_path / "state" / "drafts.sqlite3").stat().st_mode) == 0o600


def test_runtime_policy_is_built_from_validated_account_configuration() -> None:
    account = parse_config(
        {
            "accounts": [
                {
                    "id": "personal",
                    "type": "apple-mail",
                    "mail_account": "iCloud",
                    "sender_addresses": ["owner@example.com"],
                    "mailboxes": ["Inbox"],
                    "outbound_policy": {
                        "allowed_recipients": ["friend@example.net"],
                        "rate_limit_messages": 2,
                    },
                }
            ]
        }
    ).accounts[0]

    policy = OutboundPolicy.from_account(account)

    assert policy.sender_addresses == ("owner@example.com",)
    assert policy.allowed_recipients == ("friend@example.net",)
    assert policy.rate_limit_messages == 2
