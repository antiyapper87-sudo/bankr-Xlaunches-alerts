"""phase4 wallet tracking

Revision ID: 0004_phase4_wallet_tracking
Revises: 0003_phase3_user_retention
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_phase4_wallet_tracking"
down_revision = "0003_phase3_user_retention"
branch_labels = None
depends_on = None


def json_type():
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "tracked_wallets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_checked_block", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "address", "chain", name="uq_tracked_wallet_tenant_address_chain"),
    )
    op.create_index("ix_tracked_wallets_tenant_id", "tracked_wallets", ["tenant_id"])
    op.create_index("ix_tracked_wallets_address", "tracked_wallets", ["address"])
    op.create_index("ix_tracked_wallets_chain", "tracked_wallets", ["chain"])
    op.create_index("ix_tracked_wallets_status", "tracked_wallets", ["status"])
    op.create_index("ix_tracked_wallets_last_checked_at", "tracked_wallets", ["last_checked_at"])
    op.create_index("ix_tracked_wallets_status_checked", "tracked_wallets", ["status", "last_checked_at"])

    op.create_table(
        "wallet_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tracked_wallet_id", sa.Integer(), sa.ForeignKey("tracked_wallets.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("wallet_address", sa.String(length=42), nullable=False),
        sa.Column("ca", sa.String(length=42), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("amount_usd", sa.Float(), nullable=True),
        sa.Column("tx_hash", sa.String(length=128), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=True),
        sa.Column("event_json", json_type(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tx_hash", "wallet_address", "ca", "direction", name="uq_wallet_event_tx_wallet_ca_direction"),
    )
    op.create_index("ix_wallet_events_tracked_wallet_id", "wallet_events", ["tracked_wallet_id"])
    op.create_index("ix_wallet_events_tenant_id", "wallet_events", ["tenant_id"])
    op.create_index("ix_wallet_events_wallet_address", "wallet_events", ["wallet_address"])
    op.create_index("ix_wallet_events_ca", "wallet_events", ["ca"])
    op.create_index("ix_wallet_events_direction", "wallet_events", ["direction"])
    op.create_index("ix_wallet_events_block_number", "wallet_events", ["block_number"])
    op.create_index("ix_wallet_events_status", "wallet_events", ["status"])
    op.create_index("ix_wallet_events_created_at", "wallet_events", ["created_at"])
    op.create_index("ix_wallet_events_ca_created", "wallet_events", ["ca", "created_at"])
    op.create_index("ix_wallet_events_wallet_created", "wallet_events", ["wallet_address", "created_at"])


def downgrade() -> None:
    op.drop_table("wallet_events")
    op.drop_table("tracked_wallets")
