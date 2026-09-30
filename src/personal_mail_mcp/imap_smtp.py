"""Generic TLS IMAP reader and SMTP submission adapter."""

from __future__ import annotations

import asyncio
import base64
import imaplib
import re
import smtplib
import ssl
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.message import Message as EmailObject
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime
from typing import Protocol

from personal_mail_mcp.adapters import MailAdapterError, OperationContext
from personal_mail_mcp.config import ImapSmtpAccount
from personal_mail_mcp.domain import (
    AccountSummary,
    Attachment,
    AttachmentMetadata,
    BodyVariants,
    DeliveryResult,
    Draft,
    ImapMessageId,
    Mailbox,
    Message,
    MessageId,
    MessageSummary,
    Page,
    PageRequest,
    Participant,
    SearchQuery,
    ThreadReference,
)
from personal_mail_mcp.secrets import KeychainSecretProvider, SecretProvider


class ImapSmtpError(MailAdapterError):
    """A sanitized, user-actionable provider failure."""


class ImapClient(Protocol):
    def login(self, username: str, password: str) -> object: ...
    def authenticate(self, mechanism: str, authobject) -> object: ...
    def list(self) -> tuple[str, Sequence[bytes | None]]: ...
    def select(
        self, mailbox: str, readonly: bool = False
    ) -> tuple[str, Sequence[bytes]]: ...
    def response(self, code: str) -> tuple[str, Sequence[bytes]]: ...
    def uid(self, command: str, *arguments: str) -> tuple[str, Sequence[object]]: ...
    def append(
        self, mailbox: str, flags: str, date_time: None, message: bytes
    ) -> object: ...
    def logout(self) -> object: ...


class SmtpClient(Protocol):
    def login(self, username: str, password: str) -> object: ...
    def auth(self, mechanism: str, authobject) -> object: ...
    def send_message(
        self, message: EmailMessage, from_addr: str, to_addrs: Sequence[str]
    ) -> object: ...
    def quit(self) -> object: ...


ImapFactory = Callable[[str, int, float], ImapClient]
SmtpFactory = Callable[[str, int, float], SmtpClient]
AttachmentLoader = Callable[[AttachmentMetadata], bytes]


def _default_imap(host: str, port: int, timeout: float) -> ImapClient:
    return imaplib.IMAP4_SSL(
        host, port, ssl_context=ssl.create_default_context(), timeout=timeout
    )


def _default_smtp(host: str, port: int, timeout: float) -> SmtpClient:
    return smtplib.SMTP_SSL(
        host, port, timeout=timeout, context=ssl.create_default_context()
    )


def _default_smtp_starttls(host: str, port: int, timeout: float) -> SmtpClient:
    return smtplib.SMTP(host, port, timeout=timeout)


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _offset(value: str | None, length: int) -> int:
    if value is None:
        return 0
    try:
        padding = "=" * (-len(value) % 4)
        result = int(base64.b64decode(value + padding, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError):
        raise ImapSmtpError("Invalid IMAP pagination cursor.") from None
    if result < 0 or result > length:
        raise ImapSmtpError("Invalid IMAP pagination cursor.")
    return result


def _page(items: tuple, request: PageRequest) -> Page:
    start = _offset(request.cursor, len(items))
    end = min(start + request.limit, len(items))
    return Page(items[start:end], _cursor(end) if end < len(items) else None)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    # BytesParser with the default policy already exposes decoded header values.
    return str(value)


def _participants(values: Sequence[str]) -> tuple[Participant, ...]:
    result = []
    for name, address in getaddresses(values):
        if address:
            result.append(Participant(address, name or None))
    return tuple(result)


def _date(value: str | None) -> datetime:
    try:
        parsed = parsedate_to_datetime(value or "")
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        return datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(part: EmailObject) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _body(message: EmailObject) -> BodyVariants:
    text: str | None = None
    html: str | None = None
    for part in message.walk() if message.is_multipart() else (message,):
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/plain" and text is None:
            text = _text(part)
        elif part.get_content_type() == "text/html" and html is None:
            html = _text(part)
    return BodyVariants(text=text if text is not None else "", html=html)


def _attachment_metadata(message: EmailObject) -> tuple[AttachmentMetadata, ...]:
    result = []
    for index, part in enumerate(message.walk(), start=1):
        disposition = part.get_content_disposition()
        if disposition not in {"attachment", "inline"} or part.is_multipart():
            continue
        content = part.get_payload(decode=True) or b""
        result.append(
            AttachmentMetadata(
                id=str(index),
                filename=part.get_filename(),
                media_type=part.get_content_type(),
                size_bytes=len(content),
                inline=disposition == "inline",
                content_id=part.get("Content-ID"),
            )
        )
    return tuple(result)


def _thread(message: EmailObject) -> ThreadReference | None:
    references = tuple(re.findall(r"<[^>]+>", str(message.get("References", ""))))
    in_reply_to = str(message.get("In-Reply-To", "")) or None
    message_id = str(message.get("Message-ID", "")) or None
    if not references and in_reply_to is None and message_id is None:
        return None
    root = references[0] if references else in_reply_to or message_id
    return ThreadReference(root or "unknown", in_reply_to, references)


class ImapSmtpAdapter:
    """Adapter for one explicitly configured IMAP/SMTP account."""

    def __init__(
        self,
        account: ImapSmtpAccount,
        *,
        secret_provider: SecretProvider | None = None,
        keychain_service: str = "personal-mail-mcp",
        imap_factory: ImapFactory = _default_imap,
        smtp_factory: SmtpFactory = _default_smtp,
        attachment_loader: AttachmentLoader | None = None,
    ) -> None:
        self.account = account
        self._secrets = secret_provider or KeychainSecretProvider(keychain_service)
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._attachment_loader = attachment_loader

    def _check_tls(self) -> None:
        if not self.account.imap.tls or not self.account.smtp.tls:
            raise ImapSmtpError(
                "The imap-smtp adapter requires TLS for both IMAP and SMTP endpoints."
            )

    def _password(self) -> str:
        try:
            return self._secrets.resolve(self.account.secret_ref).reveal()
        except Exception:  # noqa: BLE001 - secret provider messages may contain secrets
            raise ImapSmtpError(
                f"Could not resolve the credential for account {self.account.id!r}."
            ) from None

    def _xoauth2_payload(self, credential: str) -> bytes:
        return (
            f"user={self.account.username}\x01auth=Bearer {credential}\x01\x01".encode()
        )

    def _authenticate_imap(self, client: ImapClient, credential: str) -> None:
        if self.account.auth_mode == "password":
            client.login(self.account.username, credential)
            return
        client.authenticate(
            "XOAUTH2", lambda _challenge: self._xoauth2_payload(credential)
        )

    def _authenticate_smtp(self, client: SmtpClient, credential: str) -> None:
        if self.account.auth_mode == "password":
            client.login(self.account.username, credential)
            return
        payload = self._xoauth2_payload(credential).decode()
        client.auth("XOAUTH2", lambda _challenge=None: payload)

    async def _blocking(self, function, context: OperationContext):
        context.raise_if_cancelled()
        self._check_tls()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(function), timeout=context.timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise ImapSmtpError(
                "The mail provider connection timed out. Check its host and port."
            ) from None
        except ImapSmtpError:
            raise
        except (ssl.SSLError, ssl.CertificateError):
            raise ImapSmtpError(
                "TLS validation failed for the configured mail provider. Check its host and certificate."
            ) from None
        except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
            raise ImapSmtpError(
                "Mail provider authentication failed. Verify the username and credential reference."
            ) from None
        except (OSError, smtplib.SMTPException):
            raise ImapSmtpError(
                "The configured mail provider could not complete the request. Check its TLS endpoints."
            ) from None
        except Exception:  # noqa: BLE001 - never expose provider data or credentials
            raise ImapSmtpError(
                "The configured mail provider returned an invalid response."
            ) from None
        context.raise_if_cancelled()
        return result

    def _imap(self, operation, timeout: float):
        password = self._password()
        client = self._imap_factory(
            self.account.imap.host, self.account.imap.port, timeout
        )
        try:
            self._authenticate_imap(client, password)
            return operation(client)
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001, S110 - cleanup must not mask result
                pass

    async def _with_imap(self, operation, context: OperationContext):
        return await self._blocking(
            lambda: self._imap(operation, context.timeout_seconds), context
        )

    def _select(
        self, client: ImapClient, mailbox: str, *, readonly: bool = True
    ) -> int:
        if mailbox not in self.account.mailboxes:
            raise ImapSmtpError(
                "The requested mailbox is not configured for this account."
            )
        status, _ = client.select(mailbox, readonly=readonly)
        if status != "OK":
            raise ImapSmtpError("The configured IMAP mailbox could not be selected.")
        response_status, values = client.response("UIDVALIDITY")
        if response_status != "UIDVALIDITY" or not values:
            raise ImapSmtpError("The IMAP server did not provide mailbox UIDVALIDITY.")
        return int(values[0])

    @staticmethod
    def _raw_fetch(data: Sequence[object]) -> bytes:
        for item in data:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                return item[1]
        raise ImapSmtpError("The IMAP server did not return the requested message.")

    def _fetch(self, client: ImapClient, uid: int) -> EmailObject:
        status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise ImapSmtpError("The requested IMAP message is unavailable.")
        return BytesParser(policy=policy.default).parsebytes(self._raw_fetch(data))

    def _summary(
        self, message: EmailObject, mailbox: str, validity: int, uid: int
    ) -> MessageSummary:
        body = _body(message)
        preview_source = body.text or re.sub(r"<[^>]+>", " ", body.html or "")
        preview = " ".join(preview_source.split())[:240]
        senders = _participants(message.get_all("From", []))
        if not senders:
            raise ImapSmtpError("The IMAP message has no valid sender address.")
        recipients = _participants(
            message.get_all("To", []) + message.get_all("Cc", [])
        )
        sent_at = _date(message.get("Date"))
        return MessageSummary(
            id=ImapMessageId(self.account.id, mailbox, validity, uid),
            account_id=self.account.id,
            mailbox_id=mailbox,
            subject=_decode_header(message.get("Subject")),
            sender=senders[0],
            recipients=recipients,
            sent_at=sent_at,
            received_at=sent_at,
            preview=preview,
            labels=(mailbox,),
            rfc_message_id=str(message.get("Message-ID"))
            if message.get("Message-ID")
            else None,
            thread=_thread(message),
            attachments=_attachment_metadata(message),
        )

    async def get_account(self, context: OperationContext) -> AccountSummary:
        context.raise_if_cancelled()
        self._check_tls()
        return AccountSummary(
            self.account.id, self.account.id, self.account.sender_addresses
        )

    async def list_mailboxes(
        self, page: PageRequest, context: OperationContext
    ) -> Page[Mailbox]:
        def operation(client: ImapClient) -> tuple[Mailbox, ...]:
            status, lines = client.list()
            if status != "OK":
                raise ImapSmtpError("The IMAP server could not list mailboxes.")
            available = set()
            for line in lines:
                if line:
                    match = re.search(rb'(?:"([^"]+)"|([^ ]+))$', line)
                    if match:
                        available.add((match.group(1) or match.group(2)).decode())
            return tuple(
                Mailbox(name, self.account.id, name, (name,))
                for name in self.account.mailboxes
                if name in available
            )

        return _page(await self._with_imap(operation, context), page)

    async def search_messages(
        self, query: SearchQuery, page: PageRequest, context: OperationContext
    ) -> Page[MessageSummary]:
        mailboxes = (query.mailbox_id,) if query.mailbox_id else self.account.mailboxes
        if any(value not in self.account.mailboxes for value in mailboxes):
            raise ImapSmtpError(
                "The requested mailbox is not configured for this account."
            )

        def operation(client: ImapClient) -> tuple[MessageSummary, ...]:
            result = []
            for mailbox in mailboxes:
                validity = self._select(client, mailbox)
                criteria: list[str] = ["ALL"]
                if query.text:
                    safe = query.text.replace('"', "")
                    criteria = [f'(OR SUBJECT "{safe}" BODY "{safe}")']
                status, values = client.uid("SEARCH", None, *criteria)
                if status != "OK":
                    raise ImapSmtpError("The IMAP server could not search messages.")
                raw = values[0] if values else b""
                for value in reversed(bytes(raw).split()):
                    uid = int(value)
                    summary = self._summary(
                        self._fetch(client, uid), mailbox, validity, uid
                    )
                    if (
                        query.sent_after is not None
                        and summary.sent_at < query.sent_after
                    ):
                        continue
                    if (
                        query.sent_before is not None
                        and summary.sent_at > query.sent_before
                    ):
                        continue
                    if query.labels and not set(query.labels) <= set(summary.labels):
                        continue
                    result.append(summary)
            return tuple(result)

        return _page(await self._with_imap(operation, context), page)

    def _checked_id(self, message_id: MessageId) -> ImapMessageId:
        if not isinstance(message_id, ImapMessageId):
            raise ImapSmtpError("The message reference is not an IMAP reference.")
        if message_id.account_id != self.account.id:
            raise ImapSmtpError("The message belongs to another configured account.")
        if message_id.mailbox_id not in self.account.mailboxes:
            raise ImapSmtpError(
                "The message mailbox is not configured for this account."
            )
        return message_id

    def _source(self, client: ImapClient, message_id: ImapMessageId) -> EmailObject:
        validity = self._select(client, message_id.mailbox_id)
        if validity != message_id.uid_validity:
            raise ImapSmtpError(
                "The message reference is stale because the mailbox UIDVALIDITY changed."
            )
        return self._fetch(client, message_id.uid)

    async def get_message(
        self, message_id: MessageId, context: OperationContext
    ) -> Message:
        checked = self._checked_id(message_id)

        def operation(client: ImapClient) -> Message:
            parsed = self._source(client, checked)
            return Message(
                self._summary(
                    parsed, checked.mailbox_id, checked.uid_validity, checked.uid
                ),
                _body(parsed),
            )

        return await self._with_imap(operation, context)

    async def get_attachment(
        self,
        message_id: MessageId,
        attachment_id: str,
        context: OperationContext,
    ) -> Attachment:
        checked = self._checked_id(message_id)

        def operation(client: ImapClient) -> Attachment:
            parsed = self._source(client, checked)
            for metadata in _attachment_metadata(parsed):
                if metadata.id == attachment_id:
                    part = tuple(parsed.walk())[int(attachment_id) - 1]
                    return Attachment(metadata, part.get_payload(decode=True) or b"")
            raise ImapSmtpError("The requested message attachment is unavailable.")

        return await self._with_imap(operation, context)

    def _check_draft(self, draft: Draft) -> None:
        if draft.account_id != self.account.id:
            raise ImapSmtpError("The draft belongs to another configured account.")
        if draft.sender.address.casefold() not in {
            address.casefold() for address in self.account.sender_addresses
        }:
            raise ImapSmtpError("The draft sender is not owned by this account.")

    def _message(
        self,
        draft: Draft,
        source: EmailObject | None = None,
        relation: str | None = None,
    ) -> EmailMessage:
        self._check_draft(draft)
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = draft.sender.address
        message["To"] = ", ".join(item.address for item in draft.to)
        if draft.cc:
            message["Cc"] = ", ".join(item.address for item in draft.cc)
        if draft.bcc:
            message["Bcc"] = ", ".join(item.address for item in draft.bcc)
        if draft.reply_to_address is not None:
            message["Reply-To"] = draft.reply_to_address.address
        subject = draft.subject
        if source is not None and not subject and source.get("Subject"):
            prefix = "Re: " if relation == "reply" else "Fwd: "
            subject = prefix + _decode_header(source.get("Subject"))
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid()
        if source is not None and source.get("Message-ID"):
            parent = str(source["Message-ID"])
            references = re.findall(r"<[^>]+>", str(source.get("References", "")))
            if parent not in references:
                references.append(parent)
            message["In-Reply-To"] = parent
            message["References"] = " ".join(references)
        message.set_content(draft.body.text or "")
        if draft.body.html is not None:
            message.add_alternative(draft.body.html, subtype="html")
        for metadata in draft.attachments:
            if self._attachment_loader is None:
                raise ImapSmtpError(
                    "An attachment loader is required to send attachments."
                )
            content = self._attachment_loader(metadata)
            main, _, subtype = metadata.media_type.partition("/")
            message.add_attachment(
                content,
                maintype=main or "application",
                subtype=subtype or "octet-stream",
                filename=metadata.filename,
            )
        return message

    def _deliver(
        self,
        draft: Draft,
        timeout: float,
        source: EmailObject | None = None,
        relation: str | None = None,
    ) -> DeliveryResult:
        password = self._password()
        message = self._message(draft, source, relation)
        recipients = [item.address for item in (*draft.to, *draft.cc, *draft.bcc)]
        if self.account.smtp.starttls and self._smtp_factory is _default_smtp:
            client = _default_smtp_starttls(
                self.account.smtp.host, self.account.smtp.port, timeout
            )
        else:
            client = self._smtp_factory(
                self.account.smtp.host, self.account.smtp.port, timeout
            )
        try:
            if self.account.smtp.starttls:
                starttls = getattr(client, "starttls", None)
                if not callable(starttls):
                    raise ImapSmtpError(
                        "The configured SMTP transport does not support STARTTLS."
                    )
                starttls(context=ssl.create_default_context())
            self._authenticate_smtp(client, password)
            client.send_message(message, draft.sender.address, recipients)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001, S110 - cleanup must not mask result
                pass
        return DeliveryResult(str(message["Message-ID"]), datetime.now(timezone.utc))

    async def send(self, draft: Draft, context: OperationContext) -> DeliveryResult:
        return await self._blocking(
            lambda: self._deliver(draft, context.timeout_seconds), context
        )

    async def _related(
        self,
        relation: str,
        source: MessageId,
        draft: Draft,
        context: OperationContext,
    ) -> DeliveryResult:
        checked = self._checked_id(source)

        def operation() -> DeliveryResult:
            password = self._password()
            client = self._imap_factory(
                self.account.imap.host,
                self.account.imap.port,
                context.timeout_seconds,
            )
            try:
                self._authenticate_imap(client, password)
                source_message = self._source(client, checked)
            finally:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001, S110 - cleanup must not mask result
                    pass
            return self._deliver(
                draft, context.timeout_seconds, source_message, relation
            )

        return await self._blocking(operation, context)

    async def reply(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        return await self._related("reply", source, draft, context)

    async def forward(
        self, source: MessageId, draft: Draft, context: OperationContext
    ) -> DeliveryResult:
        return await self._related("forward", source, draft, context)

    async def create_draft(self, draft: Draft, context: OperationContext) -> Draft:
        self._check_draft(draft)
        target = next(
            (name for name in self.account.mailboxes if "draft" in name.casefold()),
            None,
        )
        if target is None:
            raise ImapSmtpError("Configure a Drafts mailbox to store IMAP drafts.")
        raw = self._message(draft).as_bytes(policy=policy.SMTP)

        def operation(client: ImapClient) -> Draft:
            client.append(target, "\\Draft", None, raw)
            return draft

        return await self._with_imap(operation, context)

    async def get_draft(self, draft_id: str, context: OperationContext) -> Draft:
        context.raise_if_cancelled()
        raise ImapSmtpError(
            "Reading provider drafts by local draft ID is not supported; retain the local draft."
        )
