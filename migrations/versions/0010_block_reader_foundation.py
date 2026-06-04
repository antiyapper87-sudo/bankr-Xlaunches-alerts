"""block reader foundation

Revision ID: 0010_block_reader_foundation
Revises: 0009_feature_foundation
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_block_reader_foundation"
down_revision = "0009_feature_foundation"
branch_labels = None
depends_on = None


def json_type():
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "block_scans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("pair_address", sa.String(length=128), nullable=True),
        sa.Column("from_block", sa.Integer(), nullable=True),
        sa.Column("to_block", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=False, server_default="LOW"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default="alchemy"),
        sa.Column("summary_json", json_type(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", "provider", name="uq_block_scan_identity_provider"),
    )
    for name in ("identity_key", "pair_address", "confidence", "status"):
        op.create_index(f"ix_block_scans_{name}", "block_scans", [name])
    op.create_index("ix_block_scans_status_updated", "block_scans", ["status", "updated_at"])

    op.create_table(
        "token_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("tx_hash", sa.String(length=128), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=True),
        sa.Column("tx_index", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("wallet_address", sa.String(length=128), nullable=True),
        sa.Column("counterparty_address", sa.String(length=128), nullable=True),
        sa.Column("pair_address", sa.String(length=128), nullable=True),
        sa.Column("amount_token", sa.Float(), nullable=True),
        sa.Column("amount_native", sa.Float(), nullable=True),
        sa.Column("raw_json", json_type(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain", "tx_hash", "token_id", "event_type", "wallet_address", name="uq_token_tx_event_wallet"),
    )
    for name in ("identity_key", "block_number", "event_type", "wallet_address", "counterparty_address", "pair_address", "observed_at"):
        op.create_index(f"ix_token_transactions_{name}", "token_transactions", [name])
    op.create_index("ix_token_transactions_identity_block", "token_transactions", ["identity_key", "block_number"])

    op.create_table(
        "wallet_clusters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("cluster_key", sa.String(length=180), nullable=False, unique=True),
        sa.Column("cluster_type", sa.String(length=64), nullable=False),
        sa.Column("wallets_json", json_type(), nullable=False),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wallet_clusters_cluster_type", "wallet_clusters", ["cluster_type"])

    op.create_table(
        "bundle_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("score_impact", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("detector_version", sa.String(length=64), nullable=False, server_default="bundle-v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", "signal_type", "detector_version", name="uq_bundle_signal_identity_type_version"),
    )
    for name in ("identity_key", "signal_type", "severity"):
        op.create_index(f"ix_bundle_signals_{name}", "bundle_signals", [name])
    op.create_index("ix_bundle_signal_severity_score", "bundle_signals", ["severity", "risk_score"])

    op.create_table(
        "prebuy_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("detector_version", sa.String(length=64), nullable=False, server_default="prebuy-v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", "detector_version", name="uq_prebuy_signal_identity_version"),
    )
    for name in ("identity_key", "severity"):
        op.create_index(f"ix_prebuy_signals_{name}", "prebuy_signals", [name])

    op.create_table(
        "holder_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("holder_count", sa.Integer(), nullable=True),
        sa.Column("top_1_pct", sa.Float(), nullable=True),
        sa.Column("top_5_pct", sa.Float(), nullable=True),
        sa.Column("top_10_pct", sa.Float(), nullable=True),
        sa.Column("dev_related_pct", sa.Float(), nullable=True),
        sa.Column("fresh_wallet_pct", sa.Float(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("snapshot_json", json_type(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_holder_snapshots_identity_key", "holder_snapshots", ["identity_key"])
    op.create_index("ix_holder_snapshots_observed_at", "holder_snapshots", ["observed_at"])
    op.create_index("ix_holder_snapshots_identity_time", "holder_snapshots", ["identity_key", "observed_at"])

    op.create_table(
        "liquidity_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("tx_hash", sa.String(length=128), nullable=True),
        sa.Column("liquidity_usd", sa.Float(), nullable=True),
        sa.Column("liquidity_change_pct", sa.Float(), nullable=True),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("identity_key", "event_type", "tx_hash", "observed_at"):
        op.create_index(f"ix_liquidity_events_{name}", "liquidity_events", [name])


def downgrade() -> None:
    op.drop_table("liquidity_events")
    op.drop_table("holder_snapshots")
    op.drop_table("prebuy_signals")
    op.drop_table("bundle_signals")
    op.drop_table("wallet_clusters")
    op.drop_table("token_transactions")
    op.drop_table("block_scans")
