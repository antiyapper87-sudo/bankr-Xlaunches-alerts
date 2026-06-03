"""phase2 intelligent verdict

Revision ID: 0002_phase2_intelligent_verdict
Revises: 0001_initial_service_schema
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_phase2_intelligent_verdict"
down_revision = "0001_initial_service_schema"
branch_labels = None
depends_on = None


def json_type():
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "token_research",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ca", sa.String(length=42), sa.ForeignKey("launches.ca"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.String(length=32), nullable=False),
        sa.Column("raw_data", json_type(), nullable=False),
        sa.Column("processed_data", json_type(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ca", "requested_by", name="uq_token_research_ca_requested_by"),
    )
    op.create_index("ix_token_research_ca", "token_research", ["ca"])
    op.create_index("ix_token_research_source", "token_research", ["source"])
    op.create_index("ix_token_research_status", "token_research", ["status"])
    op.create_index("ix_token_research_status_created", "token_research", ["status", "created_at"])

    op.create_table(
        "historical_launches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ca", sa.String(length=42), nullable=False),
        sa.Column("ticker", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("deployer", sa.String(length=128), nullable=True),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_status", sa.String(length=32), nullable=True),
        sa.Column("max_mcap", sa.Float(), nullable=True),
        sa.Column("max_volume", sa.Float(), nullable=True),
        sa.Column("raw_json", json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ca", name="uq_historical_launches_ca"),
    )
    op.create_index("ix_historical_launches_ca", "historical_launches", ["ca"])
    op.create_index("ix_historical_launches_deployer", "historical_launches", ["deployer"])
    op.create_index("ix_historical_launches_final_status", "historical_launches", ["final_status"])
    op.create_index("ix_historical_launches_first_seen_at", "historical_launches", ["first_seen_at"])
    op.create_index("ix_historical_launches_launched_at", "historical_launches", ["launched_at"])
    op.create_index("ix_historical_launches_source", "historical_launches", ["source"])
    op.create_index("ix_historical_launches_ticker", "historical_launches", ["ticker"])
    op.create_index("ix_historical_ticker_seen", "historical_launches", ["ticker", "first_seen_at"])
    op.create_index("ix_historical_deployer_seen", "historical_launches", ["deployer", "first_seen_at"])

    op.create_table(
        "spoof_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ca", sa.String(length=42), sa.ForeignKey("launches.ca"), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("score_impact", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("detector_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ca", "signal_type", "detector_version", name="uq_spoof_signal_ca_type_version"),
    )
    op.create_index("ix_spoof_signals_ca", "spoof_signals", ["ca"])
    op.create_index("ix_spoof_signals_severity", "spoof_signals", ["severity"])
    op.create_index("ix_spoof_signals_signal_type", "spoof_signals", ["signal_type"])
    op.create_index("ix_spoof_ca_severity", "spoof_signals", ["ca", "severity"])

    op.create_table(
        "verdict_v2",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ca", sa.String(length=42), sa.ForeignKey("launches.ca"), nullable=False),
        sa.Column("research_id", sa.Integer(), sa.ForeignKey("token_research.id"), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("score_json", json_type(), nullable=False),
        sa.Column("verdict_json", json_type(), nullable=False),
        sa.Column("human_readable", sa.Text(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_verdict_v2_ca", "verdict_v2", ["ca"])
    op.create_index("ix_verdict_v2_created_at", "verdict_v2", ["created_at"])
    op.create_index("ix_verdict_v2_label", "verdict_v2", ["label"])
    op.create_index("ix_verdict_v2_score", "verdict_v2", ["score"])
    op.create_index("ix_verdict_v2_status", "verdict_v2", ["status"])
    op.create_index("ix_verdict_v2_ca_created", "verdict_v2", ["ca", "created_at"])
    op.create_index("ix_verdict_v2_label_score", "verdict_v2", ["label", "score"])

    op.create_table(
        "ai_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ca", sa.String(length=42), sa.ForeignKey("launches.ca"), nullable=False),
        sa.Column("verdict_v2_id", sa.Integer(), sa.ForeignKey("verdict_v2.id"), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_json", json_type(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ca", "language", "provider", "model", name="uq_ai_summary_ca_lang_provider_model"),
    )
    op.create_index("ix_ai_summaries_ca", "ai_summaries", ["ca"])
    op.create_index("ix_ai_summaries_expires_at", "ai_summaries", ["expires_at"])


def downgrade() -> None:
    op.drop_table("ai_summaries")
    op.drop_table("verdict_v2")
    op.drop_table("spoof_signals")
    op.drop_table("historical_launches")
    op.drop_table("token_research")
