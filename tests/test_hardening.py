from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from imap_smtp_mcp.diagnostics import StructuredDiagnosticFormatter
from imap_smtp_mcp.domain import AccountSummary, DeliveryResult
from imap_smtp_mcp.fake_adapter import FakeMailAdapter
from imap_smtp_mcp.outbound_service import GuardedOutboundService, OutboundPolicy


def _content(paths: list[str]) -> dict[str, object]:
    return {
        "sender": "owner@example.com",
        "to": [{"address": "friend@example.net"}],
        "cc": [],
        "bcc": [],
        "reply_to": None,
        "subject": "private",
        "body": {"text": "private body", "html": None},
        "attachment_paths": paths,
    }


def _service(root: Path, adapter: FakeMailAdapter | None = None):
    selected = adapter or FakeMailAdapter(
        AccountSummary("imap-smtp", "IMAP/SMTP", ("owner@example.com",))
    )
    return (
        GuardedOutboundService(
            {"imap-smtp": selected},
            {
                "imap-smtp": OutboundPolicy(
                    ("owner@example.com",),
                    allowed_recipients=("friend@example.net",),
                    attachment_root=root,
                )
            },
            id_factory=lambda: "draft-1",
        ),
        selected,
    )


def _run(awaitable):
    async def exercise():
        return await awaitable

    return anyio.run(exercise)


@pytest.mark.parametrize(
    "name", ["../outside.txt", ".hidden.txt", "visible/.hidden.txt"]
)
def test_attachment_traversal_and_hidden_paths_are_rejected(tmp_path, name) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("outside")
    (root / ".hidden.txt").write_text("hidden")
    (root / "visible").mkdir()
    (root / "visible" / ".hidden.txt").write_text("hidden")
    service, adapter = _service(root)

    with pytest.raises(ToolError, match="Draft content is invalid"):
        _run(
            service.execute(
                "create_draft",
                {"account_id": "imap-smtp", "content": _content([name])},
            )
        )
    assert adapter.deliveries == []


def test_symlink_escape_and_post_draft_symlink_change_are_rejected(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    link = root / "report.txt"
    link.symlink_to(outside)
    service, adapter = _service(root)
    with pytest.raises(ToolError, match="Draft content is invalid"):
        _run(
            service.execute(
                "create_draft",
                {"account_id": "imap-smtp", "content": _content([str(link)])},
            )
        )

    link.unlink()
    link.write_bytes(b"outside")
    created = _run(
        service.execute(
            "create_draft",
            {"account_id": "imap-smtp", "content": _content([str(link)])},
        )
    )
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(ToolError, match="unavailable or unsafe"):
        _run(
            service.execute(
                "send_message",
                {
                    "account_id": "imap-smtp",
                    "draft_id": created["draft_id"],
                    "confirm_send": True,
                    "idempotency_key": "unsafe",
                },
            )
        )
    assert adapter.deliveries == []


class ReadingAdapter(FakeMailAdapter):
    content_seen: bytes | None = None
    materialized_path: Path | None = None

    async def send(self, draft, context):
        del context
        self.materialized_path = Path(draft.attachments[0].id)
        self.content_seen = self.materialized_path.read_bytes()
        return DeliveryResult("sent", datetime(2026, 9, 30, tzinfo=timezone.utc))


def test_attachment_is_read_at_send_time_from_private_snapshot(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "report.txt"
    source.write_bytes(b"report")
    adapter = ReadingAdapter(
        AccountSummary("imap-smtp", "IMAP/SMTP", ("owner@example.com",))
    )
    service, _ = _service(root, adapter)
    created = _run(
        service.execute(
            "create_draft",
            {"account_id": "imap-smtp", "content": _content([str(source)])},
        )
    )
    _run(
        service.execute(
            "send_message",
            {
                "account_id": "imap-smtp",
                "draft_id": created["draft_id"],
                "confirm_send": True,
                "idempotency_key": "safe",
            },
        )
    )
    assert adapter.content_seen == b"report"
    assert adapter.materialized_path is not None
    assert not adapter.materialized_path.exists()


def test_structured_diagnostics_redact_private_fields_and_message_patterns() -> None:
    record = logging.LogRecord(
        "imap-smtp-mcp",
        logging.ERROR,
        __file__,
        1,
        "failed for owner@example.com at /private/mail/file",
        (),
        None,
    )
    record.diagnostic_fields = {
        "recipient": "friend@example.net",
        "body": "private body",
        "attachment_bytes": b"private bytes",
        "secret": "credential",
        "safe_code": "timeout",
    }
    rendered = StructuredDiagnosticFormatter().format(record)
    for private in (
        "owner@example.com",
        "friend@example.net",
        "private body",
        "private bytes",
        "credential",
        "/private/mail/file",
    ):
        assert private not in rendered
    assert "timeout" in rendered
