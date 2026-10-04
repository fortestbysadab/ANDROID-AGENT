"""Gmail over IMAP and SMTP, using only the standard library.

Chosen over the Gmail API deliberately (see docs/ARCHITECTURE.md § Email
connector). The short version: no new dependencies, no Google Cloud project,
and no refresh token that expires weekly while the agent runs unattended.

The cost is that an app password grants the whole mailbox, including delete.
This module therefore exposes **no** delete, label or settings operation. The
credential permits it; the code does not implement it.

Works unchanged against any IMAP/SMTP provider; only the default hosts are
Gmail's.
"""

from __future__ import annotations

import imaplib
import logging
import smtplib
import ssl
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from html.parser import HTMLParser

from .base import (
    DEFAULT_BODY_LIMIT,
    ChannelError,
    Message,
    tidy_text,
    truncate,
)

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DEFAULT_TIMEOUT = 25.0
#: Fetching a whole mailbox would be slow and pointless; the agent answers
#: questions about recent mail.
MAX_LIST = 25


class _HtmlToText(HTMLParser):
    """Minimal HTML stripper for messages with no text/plain part."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML must not break reading mail
        logger.info("HTML part could not be parsed; using it raw")
        return tidy_text(html)
    return tidy_text(parser.text())


def decode_header_value(raw: str | None) -> str:
    """Decode an RFC 2047 encoded-word header into real text.

    Subjects in Hindi, Bengali, Japanese or with emoji arrive as
    `=?utf-8?B?...?=`. Without this they display as that literal gibberish,
    which is the classic way an email feature fails for everyone outside
    ASCII.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw.strip()


def _part_text(part) -> str:
    """Decode one MIME part honouring its declared charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        # An unknown or misdeclared charset is common in spam and in old
        # mailers. Falling back is better than refusing to show the message.
        return payload.decode("utf-8", errors="replace")


def extract_body(message) -> str:
    """Prefer text/plain; fall back to stripped HTML. Skip attachments."""
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_part_text(part))
        elif content_type == "text/html":
            html.append(_part_text(part))
    if plain:
        return tidy_text("\n".join(plain))
    if html:
        return html_to_text("\n".join(html))
    return ""


class GmailChannel:
    """Read and send mail for one mailbox.

    `imap_factory` and `smtp_factory` exist so tests can run the whole path
    without a network or a real account.
    """

    def __init__(
        self,
        address: str,
        app_password: str,
        *,
        imap_host: str = IMAP_HOST,
        imap_port: int = IMAP_PORT,
        smtp_host: str = SMTP_HOST,
        smtp_port: int = SMTP_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        body_limit: int = DEFAULT_BODY_LIMIT,
        imap_factory=None,
        smtp_factory=None,
    ) -> None:
        if not address or "@" not in address:
            raise ValueError("A mailbox address is required")
        if not app_password:
            raise ValueError("An app password is required")
        self.address = address
        self._password = app_password
        self.imap_host, self.imap_port = imap_host, imap_port
        self.smtp_host, self.smtp_port = smtp_host, smtp_port
        self.timeout = timeout
        self.body_limit = body_limit
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or self._default_smtp

    # ---- connections -------------------------------------------------

    def _default_imap(self):
        return imaplib.IMAP4_SSL(
            self.imap_host, self.imap_port,
            ssl_context=ssl.create_default_context(), timeout=self.timeout,
        )

    def _default_smtp(self):
        return smtplib.SMTP_SSL(
            self.smtp_host, self.smtp_port,
            context=ssl.create_default_context(), timeout=self.timeout,
        )

    def _imap_login(self):
        try:
            connection = self._imap_factory()
        except OSError as exc:
            raise ChannelError(
                "Could not reach the mail server. Check the phone's network.",
                code="mail_unreachable", retryable=True,
            ) from exc
        try:
            connection.login(self.address, self._password)
        except imaplib.IMAP4.error as exc:
            # Never echo the exception text: some servers include the
            # attempted credential in their error string.
            raise ChannelError(
                "The mail server rejected the sign-in. Check the address and "
                "that the app password is still valid — Google revokes app "
                "passwords whenever the account password changes.",
                code="mail_auth_failed",
            ) from exc
        return connection

    # ---- reading -----------------------------------------------------

    def list_recent(self, *, limit: int = 10, unread_only: bool = False) -> list[Message]:
        limit = max(1, min(int(limit), MAX_LIST))
        connection = self._imap_login()
        try:
            connection.select("INBOX", readonly=True)
            criterion = "UNSEEN" if unread_only else "ALL"
            status, data = connection.search(None, criterion)
            if status != "OK":
                raise ChannelError("The mailbox could not be searched.", code="mail_search_failed")
            ids = (data[0] or b"").split()
            if not ids:
                return []
            wanted = ids[-limit:]
            messages: list[Message] = []
            for raw_id in reversed(wanted):
                # PEEK so that reading through the agent does not silently
                # mark mail as read in the owner's mailbox.
                status, payload = connection.fetch(
                    raw_id,
                    "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
                )
                if status != "OK" or not payload:
                    continue
                messages.append(self._header_message(raw_id, payload))
            return messages
        finally:
            self._close(connection)

    def fetch(self, message_id: str) -> Message:
        connection = self._imap_login()
        try:
            connection.select("INBOX", readonly=True)
            status, payload = connection.fetch(str(message_id).encode(), "(FLAGS BODY.PEEK[])")
            if status != "OK" or not payload or payload[0] is None:
                raise ChannelError(
                    f"There is no message {message_id} in the inbox.", code="mail_not_found"
                )
            raw = self._first_literal(payload)
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            body, was_truncated = truncate(extract_body(parsed), self.body_limit)
            return Message(
                message_id=str(message_id),
                sender=decode_header_value(parsed.get("From")),
                recipient=decode_header_value(parsed.get("To")),
                subject=decode_header_value(parsed.get("Subject")) or "(no subject)",
                date=decode_header_value(parsed.get("Date")),
                body=body,
                unread=self._is_unread(payload),
                truncated=was_truncated,
                extra={"rfc_message_id": (parsed.get("Message-ID") or "").strip()},
            )
        finally:
            self._close(connection)

    # ---- sending -----------------------------------------------------

    def send(
        self, *, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> str:
        _, recipient = parseaddr(to)
        if not recipient or "@" not in recipient:
            raise ChannelError(f"{to!r} is not a usable email address.", code="invalid_recipient")

        message = EmailMessage()
        message["From"] = formataddr(("", self.address))
        message["To"] = recipient
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        if in_reply_to:
            # Threading headers, so a reply lands in the original conversation
            # rather than starting a new one.
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)

        try:
            connection = self._smtp_factory()
        except OSError as exc:
            raise ChannelError(
                "Could not reach the outgoing mail server.",
                code="mail_unreachable", retryable=True,
            ) from exc
        try:
            connection.login(self.address, self._password)
            refused = connection.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise ChannelError(
                "The mail server rejected the sign-in. The app password may "
                "have been revoked.",
                code="mail_auth_failed",
            ) from exc
        except smtplib.SMTPException as exc:
            raise ChannelError(
                f"The message was not accepted: {type(exc).__name__}.",
                code="mail_send_failed", retryable=True,
            ) from exc
        finally:
            try:
                connection.quit()
            except Exception:
                logger.debug("SMTP quit failed; the send itself succeeded")

        if refused:
            # Partial acceptance still means this recipient did not get it.
            raise ChannelError(
                f"The server refused {', '.join(sorted(refused))}.",
                code="mail_recipient_refused",
            )
        return str(message["Message-ID"])

    # ---- helpers -----------------------------------------------------

    def _header_message(self, raw_id: bytes, payload) -> Message:
        raw = self._first_literal(payload)
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        return Message(
            message_id=raw_id.decode(),
            sender=decode_header_value(parsed.get("From")),
            recipient=decode_header_value(parsed.get("To")),
            subject=decode_header_value(parsed.get("Subject")) or "(no subject)",
            date=decode_header_value(parsed.get("Date")),
            body="",
            unread=self._is_unread(payload),
        )

    @staticmethod
    def _first_literal(payload) -> bytes:
        for item in payload:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        return b""

    @staticmethod
    def _is_unread(payload) -> bool:
        for item in payload:
            head = item[0] if isinstance(item, tuple) else item
            if isinstance(head, bytes) and b"\\Seen" in head:
                return False
        return True

    @staticmethod
    def _close(connection) -> None:
        for step in ("close", "logout"):
            try:
                getattr(connection, step)()
            except Exception:
                logger.debug("IMAP %s failed during teardown", step)
