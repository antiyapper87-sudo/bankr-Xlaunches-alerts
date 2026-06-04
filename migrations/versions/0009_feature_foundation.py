"""feature foundation identity outcomes lore memory

Revision ID: 0009_feature_foundation
Revises: 0008_telegram_callback_refs
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_feature_foundation"
down_revision = "0008_telegram_callback_refs"
branch_labels = None
depends_on = None


def json_type():
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "chain_token_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False, unique=True),
        sa.Column("ticker", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("launch_source", sa.String(length=64), nullable=True),
        sa.Column("source_method", sa.String(length=64), nullable=True),
        sa.Column("deployer_wallet", sa.String(length=128), nullable=True),
        sa.Column("creator_handle", sa.String(length=128), nullable=True),
        sa.Column("pair_address", sa.String(length=128), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reliable_created_at", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_confidence", sa.String(length=16), nullable=False, server_default="low"),
        sa.Column("raw_refs_json", json_type(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain", "token_id", name="uq_chain_token_identity"),
    )
    op.create_index("ix_chain_token_identities_ticker", "chain_token_identities", ["ticker"])
    op.create_index("ix_chain_token_identities_launch_source", "chain_token_identities", ["launch_source"])
    op.create_index("ix_chain_token_identities_deployer_wallet", "chain_token_identities", ["deployer_wallet"])
    op.create_index("ix_chain_token_identities_creator_handle", "chain_token_identities", ["creator_handle"])
    op.create_index("ix_chain_token_identities_pair_address", "chain_token_identities", ["pair_address"])
    op.create_index("ix_chain_token_identities_first_seen_at", "chain_token_identities", ["first_seen_at"])
    op.create_index("ix_chain_token_identities_source_created_at", "chain_token_identities", ["source_created_at"])
    op.create_index("ix_chain_token_identities_status", "chain_token_identities", ["status"])
    op.create_index("ix_chain_token_ticker_seen", "chain_token_identities", ["chain", "ticker", "first_seen_at"])
    op.create_index("ix_chain_token_source_seen", "chain_token_identities", ["chain", "launch_source", "first_seen_at"])
    op.create_index("ix_chain_token_deployer_seen", "chain_token_identities", ["chain", "deployer_wallet", "first_seen_at"])

    op.create_table(
        "token_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("initial_verdict_version", sa.String(length=64), nullable=True),
        sa.Column("initial_score", sa.Float(), nullable=True),
        sa.Column("initial_label", sa.String(length=32), nullable=True),
        sa.Column("launch_source", sa.String(length=64), nullable=True),
        sa.Column("ticker", sa.String(length=64), nullable=True),
        sa.Column("deployer_wallet", sa.String(length=128), nullable=True),
        sa.Column("pair_address", sa.String(length=128), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_1h_json", json_type(), nullable=True),
        sa.Column("snapshot_4h_json", json_type(), nullable=True),
        sa.Column("snapshot_24h_json", json_type(), nullable=True),
        sa.Column("snapshot_7d_json", json_type(), nullable=True),
        sa.Column("max_mcap_1h", sa.Float(), nullable=True),
        sa.Column("max_mcap_4h", sa.Float(), nullable=True),
        sa.Column("max_mcap_24h", sa.Float(), nullable=True),
        sa.Column("max_mcap_7d", sa.Float(), nullable=True),
        sa.Column("max_volume_24h", sa.Float(), nullable=True),
        sa.Column("min_liquidity_24h", sa.Float(), nullable=True),
        sa.Column("max_liquidity_24h", sa.Float(), nullable=True),
        sa.Column("mcap_change_1h_pct", sa.Float(), nullable=True),
        sa.Column("mcap_change_4h_pct", sa.Float(), nullable=True),
        sa.Column("mcap_change_24h_pct", sa.Float(), nullable=True),
        sa.Column("liquidity_removed_pct_24h", sa.Float(), nullable=True),
        sa.Column("holder_change_24h_pct", sa.Float(), nullable=True),
        sa.Column("rug_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dump_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pump_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dead_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("final_outcome_label", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="tracking"),
        sa.UniqueConstraint("chain", "token_id", name="uq_token_outcomes_chain_token"),
    )
    for name in ("identity_key", "launch_source", "ticker", "deployer_wallet", "pair_address", "first_seen_at", "rug_flag", "dump_flag", "pump_flag", "dead_flag", "final_outcome_label", "last_checked_at", "next_check_at", "status"):
        op.create_index(f"ix_token_outcomes_{name}", "token_outcomes", [name])
    op.create_index("ix_outcomes_due", "token_outcomes", ["status", "next_check_at"])
    op.create_index("ix_outcomes_source_label", "token_outcomes", ["launch_source", "final_outcome_label"])
    op.create_index("ix_outcomes_deployer_label", "token_outcomes", ["deployer_wallet", "final_outcome_label"])
    op.create_index("ix_outcomes_ticker_seen", "token_outcomes", ["chain", "ticker", "first_seen_at"])

    op.create_table(
        "project_lore",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("utility", sa.Text(), nullable=True),
        sa.Column("mechanics", sa.Text(), nullable=True),
        sa.Column("target_users", sa.Text(), nullable=True),
        sa.Column("meme_lore_hook", sa.Text(), nullable=True),
        sa.Column("founder_identity", sa.Text(), nullable=True),
        sa.Column("community_quality", sa.Text(), nullable=True),
        sa.Column("narrative_summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=False),
        sa.Column("lore_bullets_json", json_type(), nullable=False),
        sa.Column("attribution_type", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("attribution_confidence", sa.String(length=16), nullable=False, server_default="LOW"),
        sa.Column("ca_confirmed_evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("project_confirmed_evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ticker_context_evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_refs_json", json_type(), nullable=False),
        sa.Column("extraction_version", sa.String(length=64), nullable=False, server_default="lore-v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", "extraction_version", name="uq_project_lore_identity_version"),
    )
    op.create_index("ix_project_lore_identity_key", "project_lore", ["identity_key"])
    op.create_index("ix_project_lore_category", "project_lore", ["category"])
    op.create_index("ix_project_lore_attribution_type", "project_lore", ["attribution_type"])
    op.create_index("ix_project_lore_attribution_confidence", "project_lore", ["attribution_confidence"])
    op.create_index("ix_project_lore_created_at", "project_lore", ["created_at"])
    op.create_index("ix_project_lore_category_conf", "project_lore", ["category", "attribution_confidence"])

    op.create_table(
        "lore_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_lore_id", sa.Integer(), nullable=True),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column("excerpt_hash", sa.String(length=64), nullable=True),
        sa.Column("short_excerpt", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lore_evidence_project_lore_id", "lore_evidence", ["project_lore_id"])
    op.create_index("ix_lore_evidence_identity_key", "lore_evidence", ["identity_key"])
    op.create_index("ix_lore_evidence_evidence_type", "lore_evidence", ["evidence_type"])
    op.create_index("ix_lore_evidence_excerpt_hash", "lore_evidence", ["excerpt_hash"])
    op.create_index("ix_lore_evidence_created_at", "lore_evidence", ["created_at"])
    op.create_index("ix_lore_evidence_identity_type", "lore_evidence", ["identity_key", "evidence_type"])

    op.create_table(
        "agent_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("memory_key", sa.String(length=220), nullable=False, unique=True),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=True),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=180), nullable=False),
        sa.Column("insight", sa.Text(), nullable=False),
        sa.Column("normalized_json", json_type(), nullable=False),
        sa.Column("polarity", sa.String(length=16), nullable=False, server_default="neutral"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("reviewed_by_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    for name in ("memory_type", "chain", "subject_type", "subject_id", "last_seen_at", "expires_at", "status"):
        op.create_index(f"ix_agent_memory_{name}", "agent_memory", [name])
    op.create_index("ix_agent_memory_lookup", "agent_memory", ["subject_type", "subject_id", "status"])
    op.create_index("ix_agent_memory_type_conf", "agent_memory", ["memory_type", "confidence"])
    op.create_index("ix_agent_memory_expiry", "agent_memory", ["status", "expires_at"])

    op.create_table(
        "pattern_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pattern_key", sa.String(length=220), nullable=False, unique=True),
        sa.Column("pattern_type", sa.String(length=64), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=True),
        sa.Column("entity_key", sa.String(length=180), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rug_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pump_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_initial_score", sa.Float(), nullable=True),
        sa.Column("avg_max_mcap_24h", sa.Float(), nullable=True),
        sa.Column("avg_return_24h_pct", sa.Float(), nullable=True),
        sa.Column("score_adjustment", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )
    for name in ("pattern_type", "chain", "entity_key", "last_observed_at", "status"):
        op.create_index(f"ix_pattern_memory_{name}", "pattern_memory", [name])

    op.create_table(
        "dev_wallet_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("wallet_address", sa.String(length=128), nullable=False),
        sa.Column("wallet_key", sa.String(length=180), nullable=False, unique=True),
        sa.Column("launches_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signaled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winner_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rug_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("median_max_mcap_24h", sa.Float(), nullable=True),
        sa.Column("best_mcap", sa.Float(), nullable=True),
        sa.Column("avg_liquidity_removed_pct", sa.Float(), nullable=True),
        sa.Column("repeated_tickers_json", json_type(), nullable=False),
        sa.Column("recent_launches_json", json_type(), nullable=False),
        sa.Column("risk_label", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain", "wallet_address", name="uq_dev_wallet_chain_address"),
    )
    op.create_index("ix_dev_wallet_profiles_risk_label", "dev_wallet_profiles", ["risk_label"])
    op.create_index("ix_dev_wallet_profiles_last_seen_at", "dev_wallet_profiles", ["last_seen_at"])
    op.create_index("ix_dev_wallet_risk_conf", "dev_wallet_profiles", ["risk_label", "confidence"])

    op.create_table(
        "narrative_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=True),
        sa.Column("narrative_key", sa.String(length=128), nullable=False),
        sa.Column("examples_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winners_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rugs_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_social_score", sa.Float(), nullable=True),
        sa.Column("avg_bundle_risk", sa.Float(), nullable=True),
        sa.Column("avg_return_24h_pct", sa.Float(), nullable=True),
        sa.Column("score_adjustment", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.UniqueConstraint("chain", "narrative_key", name="uq_narrative_pattern_chain_key"),
    )
    op.create_index("ix_narrative_patterns_chain", "narrative_patterns", ["chain"])
    op.create_index("ix_narrative_patterns_narrative_key", "narrative_patterns", ["narrative_key"])

    op.create_table(
        "social_account_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(length=32), nullable=False, server_default="x"),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("account_key", sa.String(length=180), nullable=False, unique=True),
        sa.Column("mentions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ca_confirmed_mentions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winner_mentions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_mentions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suspected_paid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("template_reuse_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reliability_score", sa.Float(), nullable=False, server_default="50"),
        sa.Column("shill_risk_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_json", json_type(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.UniqueConstraint("platform", "username", name="uq_social_account_platform_username"),
    )
    op.create_index("ix_social_account_patterns_last_seen_at", "social_account_patterns", ["last_seen_at"])
    op.create_index("ix_social_account_patterns_status", "social_account_patterns", ["status"])
    op.create_index("ix_social_account_reliability", "social_account_patterns", ["reliability_score", "shill_risk_score"])

    op.create_table(
        "verdict_v3",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False, server_default="base"),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("identity_key", sa.String(length=180), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False, server_default="LOW"),
        sa.Column("output_json", json_type(), nullable=False),
        sa.Column("human_readable", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="deterministic"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default="verdict-v3-shadow"),
        sa.Column("version", sa.String(length=32), nullable=False, server_default="3.0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="shadow"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", "input_hash", "version", name="uq_verdict_v3_identity_input_version"),
    )
    for name in ("identity_key", "score", "label", "confidence", "status", "expires_at", "created_at"):
        op.create_index(f"ix_verdict_v3_{name}", "verdict_v3", [name])
    op.create_index("ix_verdict_v3_identity_created", "verdict_v3", ["identity_key", "created_at"])
    op.create_index("ix_verdict_v3_label_score", "verdict_v3", ["label", "score"])


def downgrade() -> None:
    op.drop_table("verdict_v3")
    op.drop_table("social_account_patterns")
    op.drop_table("narrative_patterns")
    op.drop_table("dev_wallet_profiles")
    op.drop_table("pattern_memory")
    op.drop_table("agent_memory")
    op.drop_table("lore_evidence")
    op.drop_table("project_lore")
    op.drop_table("token_outcomes")
    op.drop_table("chain_token_identities")
