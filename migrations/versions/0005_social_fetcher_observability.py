"""social fetcher observability

Revision ID: 0005_social_fetcher
Revises: 0004_phase4_wallet_tracking
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_social_fetcher"
down_revision = "0004_phase4_wallet_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nitter_health_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_url", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_nitter_health_logs_status", "nitter_health_logs", ["status"])
    op.create_index("ix_nitter_health_logs_created_at", "nitter_health_logs", ["created_at"])
    op.create_index("ix_nitter_health_status_created", "nitter_health_logs", ["status", "created_at"])

    op.create_table(
        "socialdata_usage_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("triggered_by_alpha", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("alpha_reason", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_socialdata_usage_logs_query_hash", "socialdata_usage_logs", ["query_hash"])
    op.create_index("ix_socialdata_usage_logs_created_at", "socialdata_usage_logs", ["created_at"])
    op.create_index("ix_socialdata_usage_created", "socialdata_usage_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("socialdata_usage_logs")
    op.drop_table("nitter_health_logs")
