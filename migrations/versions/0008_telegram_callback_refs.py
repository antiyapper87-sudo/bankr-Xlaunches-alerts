"""telegram callback refs

Revision ID: 0008_telegram_callback_refs
Revises: 0007_user_command_usage
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_telegram_callback_refs"
down_revision = "0007_user_command_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_callback_refs",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ca", sa.String(length=42), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telegram_callback_refs_kind", "telegram_callback_refs", ["kind"])
    op.create_index("ix_telegram_callback_refs_ca", "telegram_callback_refs", ["ca"])
    op.create_index("ix_telegram_callback_refs_expires_at", "telegram_callback_refs", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_telegram_callback_refs_expires_at", table_name="telegram_callback_refs")
    op.drop_index("ix_telegram_callback_refs_ca", table_name="telegram_callback_refs")
    op.drop_index("ix_telegram_callback_refs_kind", table_name="telegram_callback_refs")
    op.drop_table("telegram_callback_refs")
