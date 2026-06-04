"""phase3 user retention

Revision ID: 0003_phase3_user_retention
Revises: 0002_phase2_intelligent_verdict
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_phase3_user_retention"
down_revision = "0002_phase2_intelligent_verdict"
branch_labels = None
depends_on = None


def json_type():
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "user_watchlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("ca", sa.String(length=42), sa.ForeignKey("launches.ca"), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_mcap", sa.Float(), nullable=True),
        sa.Column("last_volume", sa.Float(), nullable=True),
        sa.Column("last_liquidity", sa.Float(), nullable=True),
        sa.Column("last_price_usd", sa.String(length=64), nullable=True),
        sa.Column("last_market_json", json_type(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "ca", name="uq_user_watchlists_tenant_ca"),
    )
    op.create_index("ix_user_watchlists_ca", "user_watchlists", ["ca"])
    op.create_index("ix_user_watchlists_tenant_id", "user_watchlists", ["tenant_id"])
    op.create_index("ix_user_watchlists_status", "user_watchlists", ["status"])
    op.create_index("ix_user_watchlists_last_checked_at", "user_watchlists", ["last_checked_at"])
    op.create_index("ix_user_watchlists_status_checked", "user_watchlists", ["status", "last_checked_at"])

    op.create_table(
        "user_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("ca", sa.String(length=42), sa.ForeignKey("launches.ca"), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("payload_json", json_type(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "ca", name="uq_user_feedback_tenant_ca"),
    )
    op.create_index("ix_user_feedback_tenant_id", "user_feedback", ["tenant_id"])
    op.create_index("ix_user_feedback_ca", "user_feedback", ["ca"])
    op.create_index("ix_user_feedback_action", "user_feedback", ["action"])
    op.create_index("ix_user_feedback_action_created", "user_feedback", ["action", "created_at"])


def downgrade() -> None:
    op.drop_table("user_feedback")
    op.drop_table("user_watchlists")
