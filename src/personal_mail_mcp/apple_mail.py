"""macOS Mail.app adapter using a fixed AppleScript program.

Provider data is passed to ``osascript`` as argv.  It is never interpolated into
the program source, which keeps message content and addresses out of executable
AppleScript.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from personal_mail_mcp.adapters import OperationContext
from personal_mail_mcp.config import AppleMailAccount
from personal_mail_mcp.domain import (
    AccountSummary,
    Attachment,
    AttachmentMetadata,
    BodyVariants,
    DeliveryResult,
    Draft,
    Mailbox,
    Message,
    MessageId,
    MessageSummary,
    Page,
    PageRequest,
    Participant,
    ProviderMessageId,
    SearchQuery,
)


class AppleMailError(RuntimeError):
    """A sanitized, user-actionable Mail.app failure."""


class AppleMailUnsupportedError(AppleMailError):
    """Raised when Mail.app automation is requested outside macOS."""


class AppleMailAutomationError(AppleMailError):
    """Raised when macOS denies permission to automate Mail.app."""


class AppleScriptRunner(Protocol):
    """Injectable subprocess boundary used by unit tests."""

    async def run(
        self, source: str, arguments: tuple[str, ...], timeout_seconds: float
    ) -> str: ...


@dataclass(frozen=True)
class SubprocessAppleScriptRunner:
    """Run a fixed program through the system ``osascript`` executable."""

    executable: str = "/usr/bin/osascript"

    async def run(
        self, source: str, arguments: tuple[str, ...], timeout_seconds: float
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "-",
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(source.encode("utf-8")), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise AppleMailError(
                "Mail.app did not respond before the timeout."
            ) from None
        except OSError:
            raise AppleMailError(
                "Could not start macOS osascript for Mail.app automation."
            ) from None

        if process.returncode:
            # Apple uses -1743 for a denied Automation consent request.  Do not
            # expose stderr: it can contain an argument or provider data.
            denied = b"-1743" in stderr or b"not authorized" in stderr.lower()
            if denied:
                raise AppleMailAutomationError(
                    "Mail.app Automation permission is required. In System Settings, "
                    "open Privacy & Security > Automation and allow the launching "
                    "terminal or Codex to control Mail."
                )
            raise AppleMailError(
                "Mail.app could not complete the requested operation. Verify the "
                "configured Mail account and mailbox names."
            )
        return stdout.decode("utf-8")


# This source is constant.  The adapter supplies operation data only after the
# ``-`` argument to osascript, where AppleScript receives it as argv.
APPLESCRIPT_SOURCE = r"""
on jsonEscape(value)
    set value to value as text
    set value to my replaceText("\\", "\\\\", value)
    set value to my replaceText(quote, "\\\"", value)
    set value to my replaceText(return, "\\n", value)
    set value to my replaceText(linefeed, "\\n", value)
    return value
end jsonEscape

on replaceText(needle, replacement, value)
    set AppleScript's text item delimiters to needle
    set pieces to text items of value
    set AppleScript's text item delimiters to replacement
    set value to pieces as text
    set AppleScript's text item delimiters to ""
    return value
end replaceText

on quoted(value)
    return quote & my jsonEscape(value) & quote
end quoted

on findAccount(accountName)
    tell application "Mail"
        if not (exists first account whose name is accountName) then error "account missing"
        return first account whose name is accountName
    end tell
end findAccount

on findMailbox(targetAccount, mailboxName)
    tell application "Mail"
        if not (exists first mailbox of targetAccount whose name is mailboxName) then error "mailbox missing"
        return first mailbox of targetAccount whose name is mailboxName
    end tell
end findMailbox

on findMessage(targetAccount, mailboxName, providerID)
    tell application "Mail"
        set targetMailbox to my findMailbox(targetAccount, mailboxName)
        if not (exists first message of targetMailbox whose id as text is providerID) then error "message missing"
        return first message of targetMailbox whose id as text is providerID
    end tell
end findMessage

on participantJSON(addressValue)
    return "{\"address\":" & my quoted(addressValue) & "}"
end participantJSON

on messageJSON(itemMessage, mailboxName, includeBody)
    tell application "Mail"
        set itemID to id of itemMessage as text
        set itemSubject to subject of itemMessage as text
        set itemSender to sender of itemMessage as text
        set sentSeconds to (date sent of itemMessage) - (date "Thursday, January 1, 1970 at 12:00:00 AM")
        set messageIDValue to message id of itemMessage as text
        set resultValue to "{\"id\":" & my quoted(itemID) & ",\"mailbox\":" & my quoted(mailboxName) & ",\"subject\":" & my quoted(itemSubject) & ",\"sender\":" & my participantJSON(itemSender) & ",\"recipients\":[],\"sent_at\":" & sentSeconds & ",\"preview\":" & my quoted(itemSubject) & ",\"rfc_message_id\":" & my quoted(messageIDValue)
        if includeBody then set resultValue to resultValue & ",\"body\":{\"text\":" & my quoted(content of itemMessage as text) & "}"
        return resultValue & "}"
    end tell
end messageJSON

on addRecipients(outgoing, recipientKind, encodedAddresses)
    if encodedAddresses is "" then return
    set AppleScript's text item delimiters to linefeed
    set addresses to text items of encodedAddresses
    set AppleScript's text item delimiters to ""
    tell application "Mail"
        repeat with addressValue in addresses
            if recipientKind is "to" then
                make new to recipient at end of to recipients of outgoing with properties {address:addressValue as text}
            else if recipientKind is "cc" then
                make new cc recipient at end of cc recipients of outgoing with properties {address:addressValue as text}
            else
                make new bcc recipient at end of bcc recipients of outgoing with properties {address:addressValue as text}
            end if
        end repeat
    end tell
end addRecipients

on populateOutgoing(outgoing, targetAccount, argv, offset)
    set senderAddress to item offset of argv
    tell application "Mail"
        if senderAddress is not in (email addresses of targetAccount) then error "sender not owned by account"
        set sender of outgoing to senderAddress
        set subject of outgoing to item (offset + 1) of argv
        set content of outgoing to item (offset + 2) of argv
    end tell
    my addRecipients(outgoing, "to", item (offset + 3) of argv)
    my addRecipients(outgoing, "cc", item (offset + 4) of argv)
    my addRecipients(outgoing, "bcc", item (offset + 5) of argv)
end populateOutgoing

on run argv
    set operation to item 1 of argv
    set accountName to item 2 of argv
    set targetAccount to my findAccount(accountName)
    tell application "Mail"
        if operation is "account" then
            return "{\"name\":" & my quoted(name of targetAccount) & "}"
        else if operation is "mailboxes" then
            set resultValue to "["
            repeat with boxItem in mailboxes of targetAccount
                if resultValue is not "[" then set resultValue to resultValue & ","
                set boxName to name of boxItem as text
                set resultValue to resultValue & "{\"id\":" & my quoted(boxName) & ",\"name\":" & my quoted(boxName) & "}"
            end repeat
            return resultValue & "]"
        else if operation is "search" then
            set mailboxName to item 3 of argv
            set needle to item 4 of argv
            set resultValue to "["
            repeat with itemMessage in messages of my findMailbox(targetAccount, mailboxName)
                if needle is "" or subject of itemMessage contains needle or content of itemMessage contains needle then
                    if resultValue is not "[" then set resultValue to resultValue & ","
                    set resultValue to resultValue & my messageJSON(itemMessage, mailboxName, false)
                end if
            end repeat
            return resultValue & "]"
        else if operation is "message" then
            return my messageJSON(my findMessage(targetAccount, item 3 of argv, item 4 of argv), item 3 of argv, true)
        else if operation is "draft" or operation is "send" then
            set outgoing to make new outgoing message with properties {visible:false}
            my populateOutgoing(outgoing, targetAccount, argv, 3)
            if operation is "send" then send outgoing
            return "{\"id\":" & my quoted(id of outgoing as text) & "}"
        else if operation is "reply" or operation is "forward" then
            set sourceMessage to my findMessage(targetAccount, item 3 of argv, item 4 of argv)
            if operation is "reply" then
                set outgoing to reply sourceMessage opening window false
            else
                set outgoing to forward sourceMessage opening window false
            end if
            my populateOutgoing(outgoing, targetAccount, argv, 5)
            send outgoing
            return "{\"id\":" & my quoted(id of outgoing as text) & "}"
        else if operation is "attachment" then
            set sourceMessage to my findMessage(targetAccount, item 3 of argv, item 4 of argv)
            set attachmentNumber to item 5 of argv as integer
            set targetAttachment to item attachmentNumber of mail attachments of sourceMessage
            save targetAttachment in POSIX file (item 6 of argv)
            return "{\"id\":" & my quoted(item 5 of argv) & ",\"filename\":" & my quoted(name of targetAttachment as text) & ",\"media_type\":\"application/octet-stream\"}"
        end if
    end tell
    error "unsupported operation"
end run
"""


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _offset(cursor: str | None, length: int) -> int:
    if cursor is None:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        offset = int(base64.b64decode(cursor + padding, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError):
        raise AppleMailError("Invalid Apple Mail pagination cursor.") from None
    if offset < 0 or offset > length:
        raise AppleMailError("Invalid Apple Mail pagination cursor.")
    return offset


def _page(items: tuple, request: PageRequest) -> Page:
    start = _offset(request.cursor, len(items))
    end = min(start + request.limit, len(items))
    return Page(items[start:end], _cursor(end) if end < len(items) else None)


class AppleMailAdapter:
    """Provider adapter scoped to one explicitly configured Mail.app account."""

    def __init__(
        self,
        account: AppleMailAccount,
        *,
        runner: AppleScriptRunner | None = None,
        platform: str | None = None,
    ) -> None:
        self.account = account
        self._runner = runner or SubprocessAppleScriptRunner()
        self._platform = platform or sys.platform

    def _available(self, context: OperationContext) -> None:
        context.raise_if_cancelled()
        if self._platform != "darwin":
            raise AppleMailUnsupportedError(
                "The apple-mail adapter requires macOS with Mail.app. Use an "
                "imap-smtp account on this platform."
            )

    async def _run(
        self, operation: str, values: tuple[str, ...], context: OperationContext
    ) -> object:
        self._available(context)
        arguments = (operation, self.account.mail_account, *values)
        try:
            raw = await self._runner.run(
                APPLESCRIPT_SOURCE, arguments, context.timeout_seconds
            )
        except AppleMailError:
            raise
        except Exception:  # noqa: BLE001 - injected runners are untrusted boundaries
            raise AppleMailError(
                "Mail.app could not complete the requested operation."
            ) from None
        context.raise_if_cancelled()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise AppleMailError(
                "Mail.app returned an unreadable response for the requested operation."
            ) from None

    def _message_id(self, mailbox_id: str, provider_id: str) -> ProviderMessageId:
        return ProviderMessageId(self.account.id, mailbox_id, provider_id)

    def _check_message_id(self, message_id: MessageId) -> ProviderMessageId:
        if not isinstance(message_id, ProviderMessageId):
            raise AppleMailError(
                "The message reference is not an Apple Mail reference."
            )
        if message_id.account_id != self.account.id:
            raise AppleMailError("The message belongs to another configured account.")
        if message_id.mailbox_id not in self.account.mailboxes:
            raise AppleMailError(
                "The message mailbox is not configured for this account."
            )
        return message_id

    async def get_account(self, context: OperationContext) -> AccountSummary:
        data = await self._run("account", (), context)
        return AccountSummary(
            self.account.id, str(data["name"]), self.account.sender_addresses
        )

    async def list_mailboxes(
        self, page: PageRequest, context: OperationContext
    ) -> Page[Mailbox]:
        data = await self._run("mailboxes", (), context)
        configured = set(self.account.mailboxes)
        mailboxes = tuple(
            Mailbox(str(item["id"]), self.account.id, str(item["name"]))
            for item in data
            if str(item["id"]) in configured
        )
        return _page(mailboxes, page)

    async def search_messages(
        self, query: SearchQuery, page: PageRequest, context: OperationContext
    ) -> Page[MessageSummary]:
        mailbox_ids = (
            (query.mailbox_id,) if query.mailbox_id else self.account.mailboxes
        )
        if any(mailbox not in self.account.mailboxes for mailbox in mailbox_ids):
            raise AppleMailError(
                "The requested mailbox is not configured for this account."
            )
        summaries: list[MessageSummary] = []
        for mailbox_id in mailbox_ids:
            data = await self._run("search", (mailbox_id, query.text or ""), context)
            summaries.extend(self._summary(item, mailbox_id) for item in data)
        filtered = tuple(
            item
            for item in summaries
            if (query.sent_after is None or item.sent_at >= query.sent_after)
            and (query.sent_before is None or item.sent_at <= query.sent_before)
            and (not query.labels or set(query.labels) <= set(item.labels))
        )
        return _page(filtered, page)

    async def get_message(
        self, message_id: MessageId, context: OperationContext
    ) -> Message:
        checked = self._check_message_id(message_id)
        data = await self._run(
            "message", (checked.mailbox_id, checked.provider_id), context
        )
        body = data.get("body", {})
        return Message(
            self._summary(data, checked.mailbox_id),
            BodyVariants(text=body.get("text"), html=body.get("html")),
        )

    async def create_draft(self, draft: Draft, context: OperationContext) -> Draft:
        self._check_draft(draft)
        await self._run("draft", self._draft_arguments(draft), context)
        return draft

    async def get_draft(self, draft_id: str, context: OperationContext) -> Draft:
        self._available(context)
        raise AppleMailError(
            "Reading Mail.app drafts is not supported; retain the locally stored draft."
        )

    async def send(self, draft: Draft, context: OperationContext) -> DeliveryResult:
        self._check_draft(draft)
        data = await self._run("send", self._draft_arguments(draft), context)
        return self._delivery(data)

    async def reply(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        return await self._related_delivery("reply", source, draft, context)

    async def forward(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        return await self._related_delivery("forward", source, draft, context)

    async def _related_delivery(
        self,
        operation: str,
        source: MessageId,
        draft: Draft,
        context: OperationContext,
    ) -> DeliveryResult:
        checked = self._check_message_id(source)
        self._check_draft(draft)
        data = await self._run(
            operation,
            (checked.mailbox_id, checked.provider_id, *self._draft_arguments(draft)),
            context,
        )
        return self._delivery(data)

    async def get_attachment(
        self,
        message_id: MessageId,
        attachment_id: str,
        context: OperationContext,
    ) -> Attachment:
        checked = self._check_message_id(message_id)
        try:
            number = int(attachment_id)
            if number < 1:
                raise ValueError
        except ValueError:
            raise AppleMailError(
                "Apple Mail attachment IDs must be positive integers."
            ) from None
        with tempfile.TemporaryDirectory(prefix="personal-mail-") as directory:
            target = Path(directory) / "attachment"
            data = await self._run(
                "attachment",
                (checked.mailbox_id, checked.provider_id, str(number), str(target)),
                context,
            )
            try:
                content = target.read_bytes()
            except OSError:
                raise AppleMailError(
                    "Mail.app did not provide the requested attachment."
                ) from None
        metadata = AttachmentMetadata(
            id=str(data.get("id", attachment_id)),
            filename=data.get("filename"),
            media_type=str(data.get("media_type", "application/octet-stream")),
            size_bytes=len(content),
        )
        return Attachment(metadata, content)

    def _summary(self, data: object, mailbox_id: str) -> MessageSummary:
        if not isinstance(data, dict):
            raise AppleMailError("Mail.app returned an unreadable message record.")
        sender = self._participant(data["sender"])
        recipients = tuple(
            self._participant(item) for item in data.get("recipients", ())
        )
        attachments = tuple(
            AttachmentMetadata(
                id=str(item["id"]),
                filename=item.get("filename"),
                media_type=str(item.get("media_type", "application/octet-stream")),
                size_bytes=int(item.get("size_bytes", 0)),
                inline=bool(item.get("inline", False)),
                content_id=item.get("content_id"),
            )
            for item in data.get("attachments", ())
        )
        sent_at = datetime.fromtimestamp(float(data["sent_at"]), timezone.utc)
        received_value = data.get("received_at")
        return MessageSummary(
            id=self._message_id(mailbox_id, str(data["id"])),
            account_id=self.account.id,
            mailbox_id=mailbox_id,
            subject=str(data.get("subject", "")),
            sender=sender,
            recipients=recipients,
            sent_at=sent_at,
            received_at=(
                datetime.fromtimestamp(float(received_value), timezone.utc)
                if received_value is not None
                else None
            ),
            preview=str(data.get("preview", "")),
            labels=tuple(str(value) for value in data.get("labels", ())),
            rfc_message_id=data.get("rfc_message_id"),
            attachments=attachments,
        )

    @staticmethod
    def _participant(data: object) -> Participant:
        if isinstance(data, str):
            name, address = parseaddr(data)
            return Participant(address=address, name=name or None)
        if not isinstance(data, dict):
            raise AppleMailError("Mail.app returned an unreadable participant record.")
        raw_address = str(data["address"])
        parsed_name, parsed_address = parseaddr(raw_address)
        return Participant(
            address=parsed_address,
            name=data.get("name") or parsed_name or None,
        )

    def _check_draft(self, draft: Draft) -> None:
        if draft.account_id != self.account.id:
            raise AppleMailError("The draft belongs to another configured account.")
        if draft.sender.address.casefold() not in {
            address.casefold() for address in self.account.sender_addresses
        }:
            raise AppleMailError("The draft sender is not owned by this account.")

    @staticmethod
    def _draft_arguments(draft: Draft) -> tuple[str, ...]:
        body = draft.body.html if draft.body.html is not None else draft.body.text or ""
        addresses = lambda values: "\n".join(item.address for item in values)
        return (
            draft.sender.address,
            draft.subject,
            body,
            addresses(draft.to),
            addresses(draft.cc),
            addresses(draft.bcc),
        )

    @staticmethod
    def _delivery(data: object) -> DeliveryResult:
        if not isinstance(data, dict) or not data.get("id"):
            raise AppleMailError("Mail.app did not return a delivery identifier.")
        return DeliveryResult(str(data["id"]), datetime.now(timezone.utc))
