"""``aslan dashboard serve`` refuses non-loopback hosts unless the
operator types the explicit ``--i-know-this-is-unsafe`` flag.

Spec §6.1 (deployment expectation) + §8.2: the v0.6.0 dashboard is
intended for localhost / SSH-tunnel use. Binding to ``0.0.0.0`` puts
the surface on the network without the proxy story (mTLS or SSO
proxy header, structured access log, integrity-protected log
shipping). The CLI refuses the bind by default; ``--i-know-this-is-unsafe``
is the operator's explicit acknowledgement that they have a
compensating proxy in front.

Tests use Click's ``CliRunner`` so uvicorn.run is never actually
invoked — every assertion is on exit code + stderr/stdout text.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from aslan_core.cli.main import cli

pytestmark = pytest.mark.integration


def _runner_env(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """Provide a CliRunner with the env wired so ``ASLAN_DASHBOARD_DSN``
    is set (so the DSN-missing branch doesn't pre-empt the bind
    check)."""
    monkeypatch.setenv("ASLAN_DASHBOARD_DSN", "postgresql://aslan_dashboard@127.0.0.1/aslan")
    return CliRunner()


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104 — passing the bind-all literal is the contract under test
        "192.168.1.1",
        "10.0.0.5",
        "::",
    ],
)
def test_serve_non_loopback_bind_without_ack_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    runner = _runner_env(monkeypatch)
    result = runner.invoke(cli, ["dashboard", "serve", "--host", host])
    assert result.exit_code != 0, result.output
    # The error text MUST mention the bypass flag — operators reading
    # the message should know how to proceed if they really do have a
    # reverse-proxy in front.
    assert "i-know-this-is-unsafe" in result.output


def test_serve_non_loopback_bind_with_ack_does_not_exit_for_bind_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``--i-know-this-is-unsafe`` the bind check passes. We
    monkey-patch ``serve`` so uvicorn.run never actually runs; the
    test just confirms the CLI did not exit for the bind reason."""
    runner = _runner_env(monkeypatch)
    called_with: dict[str, object] = {}

    def _fake_serve(*, host: str, port: int) -> None:
        called_with["host"] = host
        called_with["port"] = port

    monkeypatch.setattr("aslan_core.dashboard.serve", _fake_serve)
    bind_all = "0.0.0.0"  # noqa: S104 — exercising the unsafe-ack branch
    result = runner.invoke(
        cli,
        ["dashboard", "serve", "--host", bind_all, "--i-know-this-is-unsafe"],
    )
    assert result.exit_code == 0, result.output
    assert called_with["host"] == bind_all


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_serve_loopback_bind_does_not_require_ack(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    """Loopback hosts are the default deployment shape; no flag
    should be required."""
    runner = _runner_env(monkeypatch)
    monkeypatch.setattr("aslan_core.dashboard.serve", lambda **_: None)
    result = runner.invoke(cli, ["dashboard", "serve", "--host", host])
    assert result.exit_code == 0, result.output
