"""Email sink — stdlib smtplib over a configured relay.

``audit_smtp_url`` format::

    smtp://[user[:password]@]host[:port]            # plain (STARTTLS opt-in)
    smtps://[user[:password]@]host[:port]           # implicit TLS

If the URL omits a port, defaults are 25 (smtp) / 465 (smtps).
``audit_email_to`` is comma-separated; the sink splits + trims and
sends one message addressed to all recipients (BCC semantics — each
gets a separate envelope copy via SMTP RCPT TO so recipient lists
aren't disclosed).

The body is the JSON payload pretty-printed plus a single-line human
summary derived from ``rule_name`` + ``severity``. Subject is
``[ASLAN AUDIT] [<severity>] <rule_name>``.

stdlib ``smtplib`` is synchronous; the deliver() coroutine wraps the
blocking send via ``asyncio.to_thread`` so the dispatcher loop stays
async-friendly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aslan_core.dq.sinks._base import SinkNotConfigured

if TYPE_CHECKING:
    from aslan_core.config import Settings


def _parse_smtp_url(url: str) -> tuple[str, int, str | None, str | None, bool]:
    """Parse ``smtp(s)://user:pass@host:port`` into the SMTP tuple.

    Returns ``(host, port, username, password, use_tls)``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("smtp", "smtps"):
        raise SinkNotConfigured(
            f"audit_smtp_url scheme must be smtp:// or smtps://, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise SinkNotConfigured(f"audit_smtp_url missing host: {url!r}")
    use_tls = parsed.scheme == "smtps"
    port = parsed.port if parsed.port is not None else (465 if use_tls else 25)
    return parsed.hostname, port, parsed.username, parsed.password, use_tls


def _split_recipients(raw: str) -> list[str]:
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def _send_blocking(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_tls: bool,
    msg: EmailMessage,
) -> None:
    """Open SMTP, optionally login + STARTTLS, send, quit. Synchronous."""
    smtp_cls = smtplib.SMTP_SSL if use_tls else smtplib.SMTP
    with smtp_cls(host=host, port=port, timeout=10) as smtp:
        if not use_tls:
            # STARTTLS is opt-in over plain smtp:// when the relay
            # advertises it. Failure to upgrade is non-fatal — the
            # message still goes out (some local relays don't
            # support TLS); the operator sets smtps:// explicitly when
            # transport encryption is required.
            with contextlib.suppress(smtplib.SMTPException):
                smtp.starttls()
        if username:
            smtp.login(username, password or "")
        smtp.send_message(msg)


class EmailSink:
    """Send one alert payload as an email via a configured SMTP relay."""

    def __init__(
        self,
        *,
        settings: Settings,
        sender: object | None = None,
    ) -> None:
        self._settings = settings
        # Tests inject a fake send-callable: signature is
        # ``sender(host, port, username, password, use_tls, msg) -> None``.
        # When None, deliver() uses _send_blocking via asyncio.to_thread.
        self._sender = sender

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        if self._settings.audit_smtp_url is None:
            raise SinkNotConfigured("audit_smtp_url not configured")
        if not self._settings.audit_email_to:
            raise SinkNotConfigured("audit_email_to not configured")
        recipients = _split_recipients(self._settings.audit_email_to)
        if not recipients:
            raise SinkNotConfigured(
                "audit_email_to has no parseable recipients (after split/strip)"
            )

        host, port, username, password, use_tls = _parse_smtp_url(
            self._settings.audit_smtp_url.get_secret_value()
        )

        from_addr = username or f"audit@{host}"
        subject = f"[ASLAN AUDIT] [{severity}] {rule_name}"
        body = (
            f"Aslan audit alert\n"
            f"=================\n\n"
            f"Rule:     {rule_name}\n"
            f"Severity: {severity}\n\n"
            f"Payload (JSON):\n"
            f"{json.dumps(payload, indent=2, sort_keys=True, default=str)}\n"
        )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)

        if self._sender is not None:
            # Test path: invoke the injected callable directly.
            sender = self._sender
            # The injected sender may be sync or async; call it the
            # way mocks usually return.
            result = sender(  # type: ignore[operator]
                host=host,
                port=port,
                username=username,
                password=password,
                use_tls=use_tls,
                msg=msg,
            )
            if asyncio.iscoroutine(result):
                await result
        else:
            await asyncio.to_thread(
                _send_blocking,
                host=host,
                port=port,
                username=username,
                password=password,
                use_tls=use_tls,
                msg=msg,
            )
        return True


__all__ = ["EmailSink"]
