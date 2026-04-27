from aslan_core.db.engine import _connect_args_for, _is_pgbouncer_dsn


def test_pgbouncer_dsn_detected_by_port_6432() -> None:
    assert _is_pgbouncer_dsn("postgresql+asyncpg://u:p@host:6432/db") is True


def test_direct_postgres_dsn_detected_by_port_5432() -> None:
    assert _is_pgbouncer_dsn("postgresql+asyncpg://u:p@host:5432/db") is False


def test_no_port_defaults_to_direct() -> None:
    assert _is_pgbouncer_dsn("postgresql+asyncpg://u:p@host/db") is False


def test_connect_args_disables_statement_cache_for_pgbouncer() -> None:
    args = _connect_args_for("postgresql+asyncpg://u:p@host:6432/db")
    assert args == {"statement_cache_size": 0}


def test_connect_args_empty_for_direct() -> None:
    args = _connect_args_for("postgresql+asyncpg://u:p@host:5432/db")
    assert args == {}
