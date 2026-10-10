"""Typed input and output models for the public MCP tool contract."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    """Strict base model shared by every nested tool schema."""

    model_config = ConfigDict(extra="forbid")


class ParticipantInput(ContractModel):
    address: str = Field(description="Email address of the participant.")
    name: str | None = Field(default=None, description="Optional display name.")


class MessageContentInput(ContractModel):
    text: str | None = Field(default=None, description="Plain-text message body.")
    html: str | None = Field(default=None, description="HTML message body.")


class AttachmentInput(ContractModel):
    path: str = Field(description="Path below the configured attachment root.")
    inline: bool = Field(
        default=False,
        description="Embed in the message body instead of presenting as a download.",
    )
    content_id: str | None = Field(
        default=None,
        description="Content ID referenced by HTML as cid:CONTENT_ID; required for inline attachments.",
    )


class DraftContentInput(ContractModel):
    sender: str = Field(description="Configured sender address owned by the account.")
    to: list[ParticipantInput] = Field(description="Primary recipients.")
    cc: list[ParticipantInput] = Field(
        default_factory=list, description="Cc recipients."
    )
    bcc: list[ParticipantInput] = Field(
        default_factory=list, description="Bcc recipients."
    )
    reply_to: ParticipantInput | None = Field(
        default=None, description="Optional address for the RFC Reply-To header."
    )
    subject: str = Field(description="Message subject, which may be empty.")
    body: MessageContentInput
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Additional safe RFC email headers. Standard routing, identity, and "
            "threading headers are managed by the server and cannot be overridden."
        ),
    )
    attachments: list[AttachmentInput] = Field(
        default_factory=list,
        description="Attachments with optional inline content-ID support.",
    )
    attachment_paths: list[str] = Field(
        default_factory=list,
        description="Deprecated shorthand for non-inline attachments."
        " Policy validation occurs before reading.",
    )
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "sender": "owner@example.com",
                    "to": [{"address": "recipient@example.com", "name": "Recipient"}],
                    "subject": "Hello",
                    "body": {"text": "Plain-text body", "html": "<p>HTML body</p>"},
                    "headers": {"X-Project": "Example"},
                    "attachments": [
                        {
                            "path": "logo.png",
                            "inline": True,
                            "content_id": "company-logo",
                        }
                    ],
                }
            ]
        },
    )


class AccountItem(ContractModel):
    id: str
    display_name: str
    sender_addresses: list[str]


class AccountsResult(ContractModel):
    accounts: list[AccountItem]


class MailboxItem(ContractModel):
    id: str
    account_id: str
    display_name: str
    labels: list[str]


class MailboxesResult(ContractModel):
    mailboxes: list[MailboxItem]
    next_cursor: str | None = None


class ParticipantResult(ContractModel):
    address: str
    name: str | None = None


class AttachmentItem(ContractModel):
    id: str
    filename: str | None
    media_type: str
    size_bytes: int
    inline: bool
    content_id: str | None


class MessageItem(ContractModel):
    id: str
    account_id: str
    mailbox_id: str
    subject: str
    sender: ParticipantResult
    recipients: list[ParticipantResult]
    sent_at: str
    received_at: str | None
    preview: str
    labels: list[str]
    rfc_message_id: str | None
    thread_id: str | None
    attachments: list[AttachmentItem]


class MessagesResult(ContractModel):
    messages: list[MessageItem]
    next_cursor: str | None = None


class MessageResult(ContractModel):
    message: MessageItem
    text_body: str | None
    html_body: str | None


class DraftResult(ContractModel):
    draft_id: str
    account_id: str
    status: str


class DeliveryResult(ContractModel):
    provider_id: str
    accepted_at: str
    idempotency_key: str


class AttachmentResult(ContractModel):
    attachment: AttachmentItem
    content_base64: str | None = Field(
        default=None,
        description="Base64 content when the attachment is within the inline limit.",
    )
    download_path: str | None = Field(
        default=None,
        description="Controlled local result when content is not returned inline.",
    )


class ToolExecutor(Protocol):
    """Application-service boundary used by the MCP transport layer."""

    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...
