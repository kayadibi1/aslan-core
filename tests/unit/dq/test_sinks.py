"""Unit tests for the dq alert sinks (M3).

Each sink is exercised against a mocked transport — no httpx network
calls, no real SMTP traffic. Verifies:

  * GlitchTip sink POSTs the right URL + headers + Sentry payload shape
  * GlitchTip sink raises SinkNotConfigured when DSN is None
  * Email sink invokes the injected send-callable with the right args
    + raises SinkNotConfigured when the SMTP URL or recipient list is
    absent
  * Slack sink POSTs the webhook URL with severity-mapped colour
  * Slack sink raises SinkNotConfigured when the webhook URL is None
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from aslan_core.config import Settings
from aslan_core.dq.sinks import (
    EmailSink,
    GlitchTipSink,
    SinkNotConfigured,
    SlackSink,
    build_default_sinks,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ASLAN_PG_DSN": SecretStr("postgresql://x:y@localhost/test"),
        "ASLAN_REDIS_URL": "redis://localhost:6379/0",
        "ASLAN_S3_ENDPOINT": "http://localhost:9000",
        "ASLAN_S3_REGION": "us-east-1",
        "ASLAN_S3_ACCESS_KEY": SecretStr("x"),
        "ASLAN_S3_SECRET_KEY": SecretStr("y"),
    }
    base.update(overrides)
    return Settings(**base)


# ── GlitchTip ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_glitchtip_sink_raises_when_dsn_none() -> None:
    sink = GlitchTipSink(settings=_settings())
    with pytest.raises(SinkNotConfigured):
        await sink.deliver(payload={}, severity="error", rule_name="x")


@pytest.mark.asyncio
async def test_glitchtip_sink_posts_sentry_envelope() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = ""
    client = MagicMock()
    client.post = AsyncMock(return_value=fake_response)

    settings = _settings(
        ASLAN_AUDIT_GLITCHTIP_DSN=SecretStr("https://abckey@glitchtip.example.com:8800/123")
    )
    sink = GlitchTipSink(settings=settings, client=client)
    ok = await sink.deliver(
        payload={"source": "kap", "lag_seconds": 1850},
        severity="error",
        rule_name="recency_sla_breach",
    )
    assert ok is True
    client.post.assert_awaited_once()
    call = client.post.await_args
    url = call.args[0]
    json_body = call.kwargs["json"]
    headers = call.kwargs["headers"]
    assert url == "https://glitchtip.example.com:8800/api/123/store/"
    assert json_body["level"] == "error"
    assert json_body["tags"]["rule"] == "recency_sla_breach"
    assert json_body["tags"]["source"] == "kap"
    assert json_body["extra"]["lag_seconds"] == 1850
    assert "sentry_key=abckey" in headers["X-Sentry-Auth"]


@pytest.mark.asyncio
async def test_glitchtip_sink_severity_critical_maps_to_fatal() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = ""
    client = MagicMock()
    client.post = AsyncMock(return_value=fake_response)

    settings = _settings(
        ASLAN_AUDIT_GLITCHTIP_DSN=SecretStr("https://abckey@glitchtip.example.com/9")
    )
    sink = GlitchTipSink(settings=settings, client=client)
    await sink.deliver(payload={}, severity="critical", rule_name="x")
    body = client.post.await_args.kwargs["json"]
    assert body["level"] == "fatal"


@pytest.mark.asyncio
async def test_glitchtip_sink_propagates_4xx_as_runtimeerror() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.text = "service unavailable"
    client = MagicMock()
    client.post = AsyncMock(return_value=fake_response)

    settings = _settings(ASLAN_AUDIT_GLITCHTIP_DSN=SecretStr("https://k@host.example.com/1"))
    sink = GlitchTipSink(settings=settings, client=client)
    with pytest.raises(RuntimeError, match="503"):
        await sink.deliver(payload={}, severity="error", rule_name="x")


@pytest.mark.asyncio
async def test_glitchtip_sink_malformed_dsn_raises_sink_not_configured() -> None:
    settings = _settings(ASLAN_AUDIT_GLITCHTIP_DSN=SecretStr("not-a-url"))
    sink = GlitchTipSink(settings=settings)
    with pytest.raises(SinkNotConfigured):
        await sink.deliver(payload={}, severity="error", rule_name="x")


# ── Email ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_sink_raises_when_smtp_url_none() -> None:
    sink = EmailSink(settings=_settings())
    with pytest.raises(SinkNotConfigured):
        await sink.deliver(payload={}, severity="warn", rule_name="x")


@pytest.mark.asyncio
async def test_email_sink_raises_when_recipients_empty() -> None:
    settings = _settings(
        ASLAN_AUDIT_SMTP_URL=SecretStr("smtp://user:pw@smtp.example.com:587"),
        ASLAN_AUDIT_EMAIL_TO="",
    )
    sink = EmailSink(settings=settings)
    with pytest.raises(SinkNotConfigured):
        await sink.deliver(payload={}, severity="warn", rule_name="x")


@pytest.mark.asyncio
async def test_email_sink_invokes_sender_with_parsed_smtp() -> None:
    captured: dict[str, Any] = {}

    def fake_sender(**kwargs: Any) -> None:
        captured.update(kwargs)

    settings = _settings(
        ASLAN_AUDIT_SMTP_URL=SecretStr("smtps://alerts:secret@smtp.example.com:465"),
        ASLAN_AUDIT_EMAIL_TO="ops@aslan.example, sidar@aslan.example",
    )
    sink = EmailSink(settings=settings, sender=fake_sender)
    ok = await sink.deliver(
        payload={"source": "kap", "lag_seconds": 1234},
        severity="error",
        rule_name="recency_sla_breach",
    )
    assert ok is True
    assert captured["host"] == "smtp.example.com"
    assert captured["port"] == 465
    assert captured["username"] == "alerts"
    assert captured["password"] == "secret"
    assert captured["use_tls"] is True
    msg = captured["msg"]
    assert isinstance(msg, EmailMessage)
    assert msg["Subject"] == "[ASLAN AUDIT] [error] recency_sla_breach"
    assert "ops@aslan.example" in msg["To"]
    assert "sidar@aslan.example" in msg["To"]
    body = msg.get_content()
    assert "recency_sla_breach" in body
    assert "lag_seconds" in body


@pytest.mark.asyncio
async def test_email_sink_default_ports() -> None:
    captured: dict[str, Any] = {}

    def fake_sender(**kwargs: Any) -> None:
        captured.update(kwargs)

    settings = _settings(
        ASLAN_AUDIT_SMTP_URL=SecretStr("smtp://relay.example.com"),
        ASLAN_AUDIT_EMAIL_TO="ops@aslan.example",
    )
    sink = EmailSink(settings=settings, sender=fake_sender)
    await sink.deliver(payload={}, severity="info", rule_name="x")
    assert captured["port"] == 25
    assert captured["use_tls"] is False
    assert captured["username"] is None


@pytest.mark.asyncio
async def test_email_sink_malformed_url_raises() -> None:
    settings = _settings(
        ASLAN_AUDIT_SMTP_URL=SecretStr("ftp://wrong.example.com"),
        ASLAN_AUDIT_EMAIL_TO="ops@aslan.example",
    )
    sink = EmailSink(settings=settings, sender=lambda **_: None)
    with pytest.raises(SinkNotConfigured):
        await sink.deliver(payload={}, severity="info", rule_name="x")


@pytest.mark.asyncio
async def test_email_sink_rich_body_uses_payload_html() -> None:
    """When payload carries body_html (M6 weekly scorecard cron), the
    sink uses it verbatim and promotes the message to multipart/alternative.

    payload['subject'] overrides the generic
    '[ASLAN AUDIT] [<sev>] <rule>' subject; payload['body_text']
    becomes the text/plain alternative body.
    """
    captured: dict[str, Any] = {}

    def fake_sender(**kwargs: Any) -> None:
        captured.update(kwargs)

    settings = _settings(
        ASLAN_AUDIT_SMTP_URL=SecretStr("smtp://relay.example.com"),
        ASLAN_AUDIT_EMAIL_TO="sidar@aslan.example",
    )
    sink = EmailSink(settings=settings, sender=fake_sender)
    rich_subject = "[ASLAN AUDIT] Weekly scorecard — week of 2026-04-27"
    rich_html = "<html><body><h2>SENTINEL_HTML</h2></body></html>"
    rich_text = "SENTINEL_TEXT — plaintext fallback"
    ok = await sink.deliver(
        payload={
            "subject": rich_subject,
            "body_html": rich_html,
            "body_text": rich_text,
            "rows_written": 10,
        },
        severity="info",
        rule_name="weekly_scorecard",
    )
    assert ok is True
    msg: EmailMessage = captured["msg"]
    assert msg["Subject"] == rich_subject
    # Multipart with text/plain primary + text/html alternative.
    assert msg.is_multipart()
    parts = list(msg.walk())
    plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    html_parts = [p for p in parts if p.get_content_type() == "text/html"]
    assert plain_parts and "SENTINEL_TEXT" in plain_parts[0].get_content()
    assert html_parts and "SENTINEL_HTML" in html_parts[0].get_content()


@pytest.mark.asyncio
async def test_email_sink_falls_back_to_generic_when_no_body_html() -> None:
    """The legacy generic JSON-dump path still fires when payload has
    no body_html (covers all M3-era rules: recency_sla_breach,
    coverage_below_target, ...)."""
    captured: dict[str, Any] = {}

    def fake_sender(**kwargs: Any) -> None:
        captured.update(kwargs)

    settings = _settings(
        ASLAN_AUDIT_SMTP_URL=SecretStr("smtp://relay.example.com"),
        ASLAN_AUDIT_EMAIL_TO="ops@aslan.example",
    )
    sink = EmailSink(settings=settings, sender=fake_sender)
    await sink.deliver(
        payload={"source": "kap", "lag_seconds": 600},
        severity="error",
        rule_name="recency_sla_breach",
    )
    msg: EmailMessage = captured["msg"]
    assert msg["Subject"] == "[ASLAN AUDIT] [error] recency_sla_breach"
    # Single-part text/plain body containing the JSON dump.
    assert not msg.is_multipart()
    body = msg.get_content()
    assert "lag_seconds" in body
    assert "Aslan audit alert" in body


# ── Slack ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slack_sink_raises_when_webhook_none() -> None:
    sink = SlackSink(settings=_settings())
    with pytest.raises(SinkNotConfigured):
        await sink.deliver(payload={}, severity="error", rule_name="x")


@pytest.mark.asyncio
async def test_slack_sink_posts_attachment_with_severity_color() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = ""
    client = MagicMock()
    client.post = AsyncMock(return_value=fake_response)

    settings = _settings(
        ASLAN_AUDIT_SLACK_WEBHOOK_URL=SecretStr("https://hooks.slack.com/services/T1/B1/X")
    )
    sink = SlackSink(settings=settings, client=client)
    await sink.deliver(
        payload={"source": "kap", "lag_seconds": 1850},
        severity="error",
        rule_name="recency_sla_breach",
    )
    call = client.post.await_args
    assert call.args[0] == "https://hooks.slack.com/services/T1/B1/X"
    body = call.kwargs["json"]
    assert "recency_sla_breach" in body["text"]
    assert body["attachments"][0]["color"] == "danger"
    titles = {f["title"] for f in body["attachments"][0]["fields"]}
    assert "source" in titles
    assert "lag_seconds" in titles


@pytest.mark.asyncio
async def test_slack_sink_warn_uses_yellow() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = ""
    client = MagicMock()
    client.post = AsyncMock(return_value=fake_response)
    settings = _settings(ASLAN_AUDIT_SLACK_WEBHOOK_URL=SecretStr("https://hooks.slack.com/x"))
    sink = SlackSink(settings=settings, client=client)
    await sink.deliver(payload={}, severity="warn", rule_name="x")
    color = client.post.await_args.kwargs["json"]["attachments"][0]["color"]
    assert color == "warning"


@pytest.mark.asyncio
async def test_slack_sink_propagates_4xx() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.text = "channel not found"
    client = MagicMock()
    client.post = AsyncMock(return_value=fake_response)
    settings = _settings(ASLAN_AUDIT_SLACK_WEBHOOK_URL=SecretStr("https://hooks.slack.com/x"))
    sink = SlackSink(settings=settings, client=client)
    with pytest.raises(RuntimeError, match="400"):
        await sink.deliver(payload={}, severity="error", rule_name="x")


# ── build_default_sinks ────────────────────────────────────────────


def test_build_default_sinks_returns_three_entries() -> None:
    sinks = build_default_sinks(_settings())
    assert set(sinks) == {"glitchtip", "email", "slack"}
    assert isinstance(sinks["glitchtip"], GlitchTipSink)
    assert isinstance(sinks["email"], EmailSink)
    assert isinstance(sinks["slack"], SlackSink)
