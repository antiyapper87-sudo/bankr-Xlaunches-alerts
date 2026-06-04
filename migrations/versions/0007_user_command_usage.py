"""user command usage quota

Revision ID: 0007_user_command_usage
Revises: 0006_watchlist_ux
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_user_command_usage"
down_revision = "0006_watchlist_ux"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_command_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.String(length=64), nullable=False),
        sa.Column("command_key", sa.String(length=64), nullable=False),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_user_id", "command_key", name="uq_user_command_usage_user_command"),
    )
    op.create_index("ix_user_command_usage_user", "user_command_usage", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_command_usage_user", table_name="user_command_usage")
    op.drop_table("user_command_usage")
