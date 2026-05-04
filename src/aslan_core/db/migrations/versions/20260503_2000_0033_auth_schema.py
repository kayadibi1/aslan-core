"""auth schema — user and refresh_token tables for JWT-based authentication.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-03 20:00:00

Introduces a dedicated ``auth`` schema with two tables:

* ``auth.user`` — application users with bcrypt password hash and role.
* ``auth.refresh_token`` — rotating refresh-token families; one row per
  issued token.  Revocation is logical (``revoked`` flag) so the full
  family history is auditable.

Indexes ``rt_user_id`` and ``rt_family`` support the two most common
refresh-token lookups: enumerate all tokens for a user, and detect
token-family reuse (rotation-theft detection).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")

    op.execute("""
        CREATE TABLE auth.user (
            user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user'
                CHECK (role IN ('user', 'admin')),
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE auth.refresh_token (
            token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID NOT NULL
                REFERENCES auth.user(user_id) ON DELETE CASCADE,
            token_family  UUID NOT NULL,
            token_hash    CHAR(64) NOT NULL,
            expires_at    TIMESTAMPTZ NOT NULL,
            revoked       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("CREATE INDEX rt_user_id ON auth.refresh_token(user_id)")
    op.execute("CREATE INDEX rt_family  ON auth.refresh_token(token_family)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth.refresh_token")
    op.execute("DROP TABLE IF EXISTS auth.user")
    op.execute("DROP SCHEMA IF EXISTS auth")
