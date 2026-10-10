"""Local draft persistence and policy-guarded outbound mail workflows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import sqlite3
import stat
import tempfile
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from imap_smtp_mcp.adapters import (
    MailAdapterError,
    OperationContext,
    OutboundDeliveryAdapter,
)
from imap_smtp_mcp.config import Account
from imap_smtp_mcp.domain import (
    AttachmentMetadata,
    BodyVariants,
    DeliveryResult,
    DomainValidationError,
    Draft,
    MessageId,
    Participant,
    parse_message_id,
)


@dataclass(frozen=True)
class OutboundPolicy:
    """Per-account restrictions evaluated before external delivery."""

    sender_addresses: tuple[str, ...]
    allowed_recipients: tuple[str, ...] = ()
    allowed_recipient_domains: tuple[str, ...] = ()
    allow_any_recipient: bool = False
    attachment_root: Path | None = None
    max_attachment_bytes: int = 10 * 1024 * 1024
    rate_limit_messages: int = 20
    rate_limit_window_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.sender_addresses:
            raise ValueError("At least one sender address is required.")
        if self.max_attachment_bytes < 0:
            raise ValueError("max_attachment_bytes cannot be negative.")
        if self.rate_limit_messages < 1 or self.rate_limit_window_seconds < 1:
            raise ValueError("Rate limits must be positive.")

    @classmethod
    def from_account(cls, account: Account) -> OutboundPolicy:
        """Build the runtime policy directly from validated account config."""
        configured = account.outbound_policy
        return cls(
            sender_addresses=account.sender_addresses,
            allowed_recipients=configured.allowed_recipients,
            allowed_recipient_domains=configured.allowed_recipient_domains,
            allow_any_recipient=configured.allow_any_recipient,
            attachment_root=configured.attachment_root,
            max_attachment_bytes=configured.max_attachment_bytes,
            rate_limit_messages=configured.rate_limit_messages,
            rate_limit_window_seconds=configured.rate_limit_window_seconds,
        )


class DraftStore(Protocol):
    def put(self, draft: Draft) -> None: ...

    def get(self, draft_id: str) -> Draft | None: ...


class InMemoryDraftStore:
    """Process-local store useful for embedding and deterministic tests."""

    def __init__(self) -> None:
        self._drafts: dict[str, Draft] = {}

    def put(self, draft: Draft) -> None:
        self._drafts[draft.id] = draft

    def get(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)


class SqliteDraftStore:
    """Persistent local draft store; credentials are never accepted or stored."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._connection.commit()

    def put(self, draft: Draft) -> None:
        payload = json.dumps(_draft_json(draft), sort_keys=True, separators=(",", ":"))
        self._connection.execute(
            "INSERT OR REPLACE INTO drafts (id, payload) VALUES (?, ?)",
            (draft.id, payload),
        )
        self._connection.commit()

    def get(self, draft_id: str) -> Draft | None:
        row = self._connection.execute(
            "SELECT payload FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return _draft_from_json(json.loads(row[0])) if row is not None else None


class AuditLog:
    """Append-only JSONL audit log containing hashes, never mail content."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def append(
        self,
        *,
        timestamp: datetime,
        action: str,
        account_id: str,
        draft: Draft,
        idempotency_key: str,
        provider_result: str,
    ) -> None:
        record = {
            "timestamp": timestamp.isoformat(),
            "action": action,
            "account_id": account_id,
            "recipient_hashes": sorted(
                _hash(item.address.casefold())
                for item in (*draft.to, *draft.cc, *draft.bcc)
            ),
            "subject_hash": _hash(draft.subject),
            "idempotency_key": idempotency_key,
            "provider_result": provider_result,
        }
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(
                descriptor,
                (
                    json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode(),
            )
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class _IdempotentResult:
    fingerprint: str
    result: DeliveryResult


class GuardedOutboundService:
    """Execute draft and delivery tools behind one mandatory policy path."""

    def __init__(
        self,
        adapters: Mapping[str, OutboundDeliveryAdapter],
        policies: Mapping[str, OutboundPolicy],
        *,
        draft_store: DraftStore | None = None,
        audit_log: AuditLog | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if set(adapters) != set(policies):
            raise ValueError("Every outbound adapter requires exactly one policy.")
        self._adapters = dict(adapters)
        self._policies = dict(policies)
        self._drafts = draft_store or InMemoryDraftStore()
        self._audit = audit_log
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._idempotency: dict[tuple[str, str], _IdempotentResult] = {}
        self._deliveries: dict[str, deque[datetime]] = {
            account_id: deque() for account_id in adapters
        }
        self._lock = asyncio.Lock()

    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "create_draft":
            return self._create_draft(arguments)
        if tool_name == "update_draft_headers":
            return self._update_draft_headers(arguments)
        if tool_name not in {"send_message", "reply_to_message", "forward_message"}:
            raise ToolError(
                f"Tool {tool_name!r} is not available in the outbound service."
            )
        return await self._deliver(tool_name, arguments)

    def _create_draft(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        account_id, _adapter, policy = self._account(arguments)
        draft = self._draft(
            account_id,
            arguments.get("content"),
            policy=policy,
            reply_to=self._source(arguments.get("reply_to_message_id"), account_id),
            forward_of=self._source(arguments.get("forward_of_message_id"), account_id),
        )
        if draft.reply_to is not None and draft.forward_of is not None:
            raise ToolError("A draft cannot be both a reply and a forward.")
        self._drafts.put(draft)
        return {"draft_id": draft.id, "account_id": account_id, "status": "draft"}

    def _update_draft_headers(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        account_id, _adapter, _policy = self._account(arguments)
        draft_id = arguments.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ToolError("A non-empty draft_id is required.")
        draft = self._drafts.get(draft_id)
        if draft is None or draft.account_id != account_id:
            raise ToolError("The requested draft was not found.")
        try:
            headers = _headers(arguments.get("headers"), allow_remove=True)
        except (TypeError, ValueError):
            raise ToolError("Draft headers are invalid or protected.") from None
        values = dict(draft.headers)
        for name, value in headers.items():
            if value is None:
                values.pop(name, None)
            else:
                values[name] = value
        updated = Draft(
            id=draft.id,
            account_id=draft.account_id,
            sender=draft.sender,
            to=draft.to,
            cc=draft.cc,
            bcc=draft.bcc,
            subject=draft.subject,
            body=draft.body,
            created_at=draft.created_at,
            reply_to_address=draft.reply_to_address,
            reply_to=draft.reply_to,
            forward_of=draft.forward_of,
            attachments=draft.attachments,
            headers=tuple(sorted(values.items())),
        )
        self._drafts.put(updated)
        return {"draft_id": updated.id, "account_id": account_id, "status": "draft"}

    async def _deliver(
        self, action: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        account_id, adapter, policy = self._account(arguments)
        if arguments.get("confirm_send") is not True:
            raise ToolError("External delivery requires confirm_send=true.")
        key = arguments.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            raise ToolError("A non-empty idempotency_key is required.")

        if action == "send_message":
            draft_id = arguments.get("draft_id")
            if not isinstance(draft_id, str) or not draft_id.strip():
                raise ToolError("A non-empty draft_id is required.")
            draft = self._drafts.get(draft_id)
            if draft is None or draft.account_id != account_id:
                raise ToolError("The local draft was not found for this account.")
            source = draft.reply_to or draft.forward_of
        else:
            source = self._source(arguments.get("message_id"), account_id)
            assert source is not None
            draft = self._draft(
                account_id,
                arguments.get("content"),
                policy=policy,
                reply_to=source if action == "reply_to_message" else None,
                forward_of=source if action == "forward_message" else None,
            )

        fingerprint = _fingerprint(action, account_id, draft, source)
        cache_key = (account_id, key)
        async with self._lock:
            previous = self._idempotency.get(cache_key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ToolError(
                        "The idempotency key was already used for another action."
                    )
                return _delivery(previous.result, key)

            self._validate_policy(draft, policy)
            now = self._clock()
            history = self._deliveries[account_id]
            cutoff = now - timedelta(seconds=policy.rate_limit_window_seconds)
            while history and history[0] <= cutoff:
                history.popleft()
            if len(history) >= policy.rate_limit_messages:
                raise ToolError("The configured account rate limit has been reached.")

            context = OperationContext(self._timeout_seconds)
            try:
                with _MaterializedDraft(draft, policy) as delivery_draft:
                    if draft.reply_to is not None:
                        result = await adapter.reply(
                            draft.reply_to, delivery_draft, context
                        )
                    elif draft.forward_of is not None:
                        result = await adapter.forward(
                            draft.forward_of, delivery_draft, context
                        )
                    else:
                        result = await adapter.send(delivery_draft, context)
            except ToolError:
                raise
            except MailAdapterError as error:
                raise ToolError(str(error)) from None
            except Exception:  # noqa: BLE001
                raise ToolError(
                    "The mail provider could not deliver the message."
                ) from None

            history.append(now)
            self._idempotency[cache_key] = _IdempotentResult(fingerprint, result)
            if self._audit is not None:
                self._audit.append(
                    timestamp=now,
                    action=action,
                    account_id=account_id,
                    draft=draft,
                    idempotency_key=key,
                    provider_result=_hash(result.provider_id),
                )
            return _delivery(result, key)

    def _account(self, arguments: Mapping[str, Any]):
        account_id = arguments.get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ToolError("A configured account_id is required.")
        try:
            return account_id, self._adapters[account_id], self._policies[account_id]
        except KeyError:
            raise ToolError(f"Unknown account ID {account_id!r}.") from None

    def _draft(
        self,
        account_id: str,
        content: Any,
        *,
        policy: OutboundPolicy,
        reply_to: MessageId | None = None,
        forward_of: MessageId | None = None,
    ) -> Draft:
        if not isinstance(content, Mapping):
            raise ToolError("Draft content is required.")
        try:
            attachments = tuple(
                _attachment(value, policy.attachment_root)
                for value in [
                    *({"path": path} for path in content.get("attachment_paths", ())),
                    *content.get("attachments", ()),
                ]
            )
            return Draft(
                id=self._id_factory(),
                account_id=account_id,
                sender=Participant(str(content.get("sender", ""))),
                to=_participants(content.get("to", ())),
                cc=_participants(content.get("cc", ())),
                bcc=_participants(content.get("bcc", ())),
                reply_to_address=_optional_participant(content.get("reply_to")),
                subject=str(content.get("subject", "")),
                body=_body(content.get("body")),
                created_at=self._clock(),
                reply_to=reply_to,
                forward_of=forward_of,
                attachments=attachments,
                headers=tuple(sorted(_headers(content.get("headers", {})).items())),
            )
        except (DomainValidationError, OSError, TypeError, ValueError):
            raise ToolError("Draft content is invalid.") from None

    @staticmethod
    def _source(value: Any, account_id: str) -> MessageId | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ToolError("Malformed message identifier.")
        try:
            source = parse_message_id(value)
        except DomainValidationError as error:
            raise ToolError(str(error)) from None
        if source.account_id != account_id:
            raise ToolError("The message reference belongs to another account.")
        return source

    @staticmethod
    def _validate_policy(draft: Draft, policy: OutboundPolicy) -> None:
        if draft.sender.address.casefold() not in {
            item.casefold() for item in policy.sender_addresses
        }:
            raise ToolError("The sender address is not owned by this account.")
        recipients = (*draft.to, *draft.cc, *draft.bcc)
        if not recipients:
            raise ToolError("At least one recipient is required.")
        if not policy.allow_any_recipient:
            allowed = {item.casefold() for item in policy.allowed_recipients}
            domains = {
                item.casefold().removeprefix("@")
                for item in policy.allowed_recipient_domains
            }
            for recipient in recipients:
                address = recipient.address.casefold()
                domain = address.rsplit("@", 1)[-1]
                if address not in allowed and domain not in domains:
                    raise ToolError(
                        "A recipient is not permitted by the account allowlist."
                    )
        if draft.attachments and policy.attachment_root is None:
            raise ToolError("Local attachments require a configured attachment_root.")


def _participants(values: Any) -> tuple[Participant, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError
    return tuple(_optional_participant(item, required=True) for item in values)  # type: ignore[misc]


def _optional_participant(value: Any, *, required: bool = False) -> Participant | None:
    if value is None and not required:
        return None
    if not isinstance(value, Mapping):
        raise TypeError
    return Participant(str(value.get("address", "")), value.get("name"))


def _body(value: Any) -> BodyVariants:
    if not isinstance(value, Mapping):
        raise TypeError
    return BodyVariants(text=value.get("text"), html=value.get("html"))


def _attachment(value: Any, root: Path | None) -> AttachmentMetadata:
    if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
        raise TypeError
    if root is None:
        raise ValueError
    path, relative = _attachment_path(value["path"], root)
    size = path.stat().st_size
    inline = value.get("inline", False)
    content_id = value.get("content_id")
    if not isinstance(inline, bool) or (
        content_id is not None and not isinstance(content_id, str)
    ):
        raise ValueError
    if inline and (not content_id or "\r" in content_id or "\n" in content_id):
        raise ValueError
    return AttachmentMetadata(
        id=relative.as_posix(),
        filename=path.name,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        size_bytes=size,
        inline=inline,
        content_id=content_id,
    )


_RESERVED_HEADERS = {
    "bcc",
    "cc",
    "content-type",
    "date",
    "from",
    "in-reply-to",
    "message-id",
    "mime-version",
    "references",
    "reply-to",
    "sender",
    "subject",
    "to",
}


def _headers(value: Any, *, allow_remove: bool = False) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        raise TypeError
    result: dict[str, str | None] = {}
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or not name
            or any(char in name for char in "\r\n:")
            or name.casefold() in _RESERVED_HEADERS
            or (item is None and not allow_remove)
            or (
                item is not None
                and (not isinstance(item, str) or "\r" in item or "\n" in item)
            )
        ):
            raise ValueError
        result[name] = item
    return result


def _attachment_path(value: str, root: Path) -> tuple[Path, Path]:
    """Resolve one visible regular file below a non-symlink configured root."""
    configured_root = root.expanduser()
    if not configured_root.is_absolute():
        raise ValueError
    resolved_root = configured_root.resolve(strict=True)
    if configured_root.is_symlink() or not resolved_root.is_dir():
        raise ValueError
    supplied = Path(value).expanduser()
    candidate = supplied if supplied.is_absolute() else configured_root / supplied
    try:
        relative = candidate.relative_to(configured_root)
    except ValueError:
        raise ValueError from None
    if not relative.parts or any(
        part in {".", ".."} or part.startswith(".") for part in relative.parts
    ):
        raise ValueError
    current = configured_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise ValueError from None
    if not resolved.is_file():
        raise ValueError
    return resolved, relative


class _MaterializedDraft:
    """Securely snapshot attachments only for the duration of provider delivery."""

    def __init__(self, draft: Draft, policy: OutboundPolicy) -> None:
        self._draft = draft
        self._policy = policy
        self._temporary: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Draft:
        if not self._draft.attachments:
            return self._draft
        root = self._policy.attachment_root
        if root is None:
            raise ToolError("Local attachments require a configured attachment_root.")
        self._temporary = tempfile.TemporaryDirectory(prefix="imap-smtp-mcp-")
        os.chmod(self._temporary.name, 0o700)
        materialized: list[AttachmentMetadata] = []
        total = 0
        try:
            for index, metadata in enumerate(self._draft.attachments):
                remaining = self._policy.max_attachment_bytes - total
                content = _secure_read(root, metadata.id, remaining)
                if len(content) != metadata.size_bytes:
                    raise ToolError(
                        "An attachment changed after the draft was created."
                    )
                total += len(content)
                if total > self._policy.max_attachment_bytes:
                    raise ToolError(
                        "Attachments exceed the configured combined size limit."
                    )
                target = Path(self._temporary.name) / f"{index}-{metadata.filename}"
                descriptor = os.open(
                    target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                try:
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                finally:
                    os.close(descriptor)
                materialized.append(
                    AttachmentMetadata(
                        id=str(target),
                        filename=metadata.filename,
                        media_type=metadata.media_type,
                        size_bytes=len(content),
                        inline=metadata.inline,
                        content_id=metadata.content_id,
                    )
                )
        except Exception:
            self.__exit__(None, None, None)
            raise
        return Draft(
            id=self._draft.id,
            account_id=self._draft.account_id,
            sender=self._draft.sender,
            to=self._draft.to,
            cc=self._draft.cc,
            bcc=self._draft.bcc,
            subject=self._draft.subject,
            body=self._draft.body,
            created_at=self._draft.created_at,
            reply_to_address=self._draft.reply_to_address,
            reply_to=self._draft.reply_to,
            forward_of=self._draft.forward_of,
            attachments=tuple(materialized),
            headers=self._draft.headers,
        )

    def __exit__(self, *_exc: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()


def _secure_read(root: Path, relative_value: str, max_bytes: int) -> bytes:
    """Open every path component without following symlinks."""
    relative = Path(relative_value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {".", ".."} or part.startswith(".") for part in relative.parts)
    ):
        raise ToolError("An attachment path is outside the configured file root.")
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in relative.parts[:-1]:
            descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            descriptors.append(descriptor)
        file_descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
        descriptors.append(file_descriptor)
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise OSError
        chunks: list[bytes] = []
        byte_count = 0
        while chunk := os.read(file_descriptor, 1024 * 1024):
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise ToolError(
                    "Attachments exceed the configured combined size limit."
                )
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise ToolError(
            "An attachment is unavailable or unsafe at send time."
        ) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _delivery(result: DeliveryResult, key: str) -> dict[str, Any]:
    return {
        "provider_id": result.provider_id,
        "accepted_at": result.accepted_at.isoformat(),
        "idempotency_key": key,
    }


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fingerprint(
    action: str, account_id: str, draft: Draft, source: MessageId | None
) -> str:
    draft_value = _draft_json(draft)
    # Generated local identity and timestamps are not part of the caller's
    # request, so a direct reply/forward retry must not depend on them.
    draft_value.pop("id")
    draft_value.pop("created_at")
    value = {
        "action": action,
        "account": account_id,
        "draft": draft_value,
        "source": source.serialize() if source else None,
    }
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _draft_json(draft: Draft) -> dict[str, Any]:
    participant = lambda item: {"address": item.address, "name": item.name}
    return {
        "id": draft.id,
        "account_id": draft.account_id,
        "sender": participant(draft.sender),
        "to": [participant(item) for item in draft.to],
        "cc": [participant(item) for item in draft.cc],
        "bcc": [participant(item) for item in draft.bcc],
        "reply_to_address": participant(draft.reply_to_address)
        if draft.reply_to_address
        else None,
        "subject": draft.subject,
        "body": {"text": draft.body.text, "html": draft.body.html},
        "created_at": draft.created_at.isoformat(),
        "reply_to": draft.reply_to.serialize() if draft.reply_to else None,
        "forward_of": draft.forward_of.serialize() if draft.forward_of else None,
        "attachments": [
            {
                "id": item.id,
                "filename": item.filename,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "inline": item.inline,
                "content_id": item.content_id,
            }
            for item in draft.attachments
        ],
        "headers": dict(draft.headers),
    }


def _draft_from_json(value: Mapping[str, Any]) -> Draft:
    participant = lambda item: Participant(item["address"], item.get("name"))
    return Draft(
        id=value["id"],
        account_id=value["account_id"],
        sender=participant(value["sender"]),
        to=tuple(participant(item) for item in value["to"]),
        cc=tuple(participant(item) for item in value["cc"]),
        bcc=tuple(participant(item) for item in value["bcc"]),
        reply_to_address=(
            participant(value["reply_to_address"])
            if value["reply_to_address"]
            else None
        ),
        subject=value["subject"],
        body=BodyVariants(**value["body"]),
        created_at=datetime.fromisoformat(value["created_at"]),
        reply_to=parse_message_id(value["reply_to"]) if value["reply_to"] else None,
        forward_of=parse_message_id(value["forward_of"])
        if value["forward_of"]
        else None,
        attachments=tuple(AttachmentMetadata(**item) for item in value["attachments"]),
        headers=tuple(sorted(value.get("headers", {}).items())),
    )
