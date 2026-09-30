"""Provider-neutral mail domain models and stable identifiers."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class DomainValidationError(ValueError):
    """Raised when provider data cannot form a valid domain object."""


def _required(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string.")


def _aware(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DomainValidationError(f"{field_name} must include a timezone.")


def _encode_identifier(kind: str, values: dict[str, object]) -> str:
    raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"personal-mail:{kind}:{encoded}"


def _decode_identifier(value: str, expected_kind: str) -> dict[str, object]:
    try:
        prefix, kind, encoded = value.split(":", 2)
        if prefix != "personal-mail" or kind != expected_kind:
            raise ValueError
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        result = json.loads(decoded)
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise DomainValidationError(
            f"Malformed {expected_kind} message identifier."
        ) from None


@dataclass(frozen=True)
class ProviderMessageId:
    """Stable identifier for a provider message that is not IMAP-backed."""

    account_id: str
    mailbox_id: str
    provider_id: str

    def __post_init__(self) -> None:
        _required(self.account_id, "account_id")
        _required(self.mailbox_id, "mailbox_id")
        _required(self.provider_id, "provider_id")

    def serialize(self) -> str:
        return _encode_identifier(
            "provider",
            {
                "account": self.account_id,
                "mailbox": self.mailbox_id,
                "id": self.provider_id,
            },
        )

    @classmethod
    def parse(cls, value: str) -> ProviderMessageId:
        data = _decode_identifier(value, "provider")
        try:
            return cls(
                account_id=data["account"],
                mailbox_id=data["mailbox"],
                provider_id=data["id"],
            )
        except (KeyError, TypeError, DomainValidationError):
            raise DomainValidationError(
                "Malformed provider message identifier."
            ) from None


@dataclass(frozen=True)
class ImapMessageId:
    """Stable IMAP identity; UIDVALIDITY prevents unsafe UID reuse."""

    account_id: str
    mailbox_id: str
    uid_validity: int
    uid: int

    def __post_init__(self) -> None:
        _required(self.account_id, "account_id")
        _required(self.mailbox_id, "mailbox_id")
        if (
            isinstance(self.uid_validity, bool)
            or not isinstance(self.uid_validity, int)
            or self.uid_validity < 1
        ):
            raise DomainValidationError("uid_validity must be a positive integer.")
        if isinstance(self.uid, bool) or not isinstance(self.uid, int) or self.uid < 1:
            raise DomainValidationError("uid must be a positive integer.")

    def serialize(self) -> str:
        return _encode_identifier(
            "imap",
            {
                "account": self.account_id,
                "mailbox": self.mailbox_id,
                "uid_validity": self.uid_validity,
                "uid": self.uid,
            },
        )

    @classmethod
    def parse(cls, value: str) -> ImapMessageId:
        data = _decode_identifier(value, "imap")
        try:
            return cls(
                account_id=data["account"],
                mailbox_id=data["mailbox"],
                uid_validity=data["uid_validity"],
                uid=data["uid"],
            )
        except (KeyError, TypeError, DomainValidationError):
            raise DomainValidationError("Malformed imap message identifier.") from None


MessageId = ProviderMessageId | ImapMessageId


def parse_message_id(value: str) -> MessageId:
    """Parse either supported serialized message identifier."""
    if value.startswith("personal-mail:imap:"):
        return ImapMessageId.parse(value)
    if value.startswith("personal-mail:provider:"):
        return ProviderMessageId.parse(value)
    raise DomainValidationError("Malformed message identifier.")


@dataclass(frozen=True)
class Participant:
    address: str
    name: str | None = None

    def __post_init__(self) -> None:
        if not _EMAIL_RE.fullmatch(self.address):
            raise DomainValidationError("participant address is invalid.")
        if self.name is not None and not self.name.strip():
            raise DomainValidationError("participant name cannot be blank.")


@dataclass(frozen=True)
class BodyVariants:
    text: str | None = None
    html: str | None = None

    def __post_init__(self) -> None:
        if self.text is None and self.html is None:
            raise DomainValidationError(
                "at least one message body variant is required."
            )


@dataclass(frozen=True)
class ThreadReference:
    thread_id: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.thread_id, "thread_id")


@dataclass(frozen=True)
class AttachmentMetadata:
    id: str
    filename: str | None
    media_type: str
    size_bytes: int
    inline: bool = False
    content_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.id, "attachment id")
        _required(self.media_type, "attachment media_type")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise DomainValidationError("attachment size_bytes cannot be negative.")


@dataclass(frozen=True)
class AccountSummary:
    id: str
    display_name: str
    sender_addresses: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.id, "account id")
        _required(self.display_name, "account display_name")
        if not self.sender_addresses or any(
            not _EMAIL_RE.fullmatch(address) for address in self.sender_addresses
        ):
            raise DomainValidationError("account sender_addresses must be valid.")


@dataclass(frozen=True)
class Mailbox:
    id: str
    account_id: str
    display_name: str
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.id, "mailbox id")
        _required(self.account_id, "mailbox account_id")
        _required(self.display_name, "mailbox display_name")


@dataclass(frozen=True)
class MessageSummary:
    id: MessageId
    account_id: str
    mailbox_id: str
    subject: str
    sender: Participant
    recipients: tuple[Participant, ...]
    sent_at: datetime
    received_at: datetime | None
    preview: str
    labels: tuple[str, ...] = ()
    rfc_message_id: str | None = None
    thread: ThreadReference | None = None
    attachments: tuple[AttachmentMetadata, ...] = ()

    def __post_init__(self) -> None:
        _required(self.account_id, "message account_id")
        _required(self.mailbox_id, "message mailbox_id")
        if (
            self.id.account_id != self.account_id
            or self.id.mailbox_id != self.mailbox_id
        ):
            raise DomainValidationError(
                "message identifier scope does not match message."
            )
        _aware(self.sent_at, "sent_at")
        if self.received_at is not None:
            _aware(self.received_at, "received_at")


@dataclass(frozen=True)
class Message:
    summary: MessageSummary
    body: BodyVariants


@dataclass(frozen=True)
class Draft:
    id: str
    account_id: str
    sender: Participant
    to: tuple[Participant, ...]
    cc: tuple[Participant, ...]
    bcc: tuple[Participant, ...]
    subject: str
    body: BodyVariants
    created_at: datetime
    reply_to: MessageId | None = None
    forward_of: MessageId | None = None
    attachments: tuple[AttachmentMetadata, ...] = ()

    def __post_init__(self) -> None:
        _required(self.id, "draft id")
        _required(self.account_id, "draft account_id")
        _aware(self.created_at, "created_at")
        if self.reply_to is not None and self.reply_to.account_id != self.account_id:
            raise DomainValidationError("reply source belongs to another account.")
        if (
            self.forward_of is not None
            and self.forward_of.account_id != self.account_id
        ):
            raise DomainValidationError("forward source belongs to another account.")


@dataclass(frozen=True)
class Attachment:
    metadata: AttachmentMetadata
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.content) != self.metadata.size_bytes:
            raise DomainValidationError(
                "attachment content size does not match metadata."
            )


@dataclass(frozen=True)
class DeliveryResult:
    provider_id: str
    accepted_at: datetime

    def __post_init__(self) -> None:
        _required(self.provider_id, "delivery provider_id")
        _aware(self.accepted_at, "accepted_at")


@dataclass(frozen=True)
class SearchQuery:
    text: str | None = None
    mailbox_id: str | None = None
    labels: tuple[str, ...] = ()
    sent_after: datetime | None = None
    sent_before: datetime | None = None

    def __post_init__(self) -> None:
        if self.sent_after is not None:
            _aware(self.sent_after, "sent_after")
        if self.sent_before is not None:
            _aware(self.sent_before, "sent_before")
        if (
            self.sent_after is not None
            and self.sent_before is not None
            and self.sent_after > self.sent_before
        ):
            raise DomainValidationError("sent_after cannot be later than sent_before.")


@dataclass(frozen=True)
class PageRequest:
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise DomainValidationError("page limit must be from 1 to 100.")


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None = None
