"""watchlist ux metrics

Revision ID: 0006_watchlist_ux
Revises: 0005_social_fetcher
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_watchlist_ux"
down_revision = "0005_social_fetcher"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_watchlists", sa.Column("initial_mcap", sa.Float(), nullable=True))
    op.add_column("user_watchlists", sa.Column("initial_volume", sa.Float(), nullable=True))
    op.add_column("user_watchlists", sa.Column("initial_liquidity", sa.Float(), nullable=True))
    op.add_column("user_watchlists", sa.Column("previous_mcap", sa.Float(), nullable=True))
    op.add_column("user_watchlists", sa.Column("previous_volume", sa.Float(), nullable=True))
    op.add_column("user_watchlists", sa.Column("last_mcap_change_pct", sa.Float(), nullable=True))
    op.add_column("user_watchlists", sa.Column("last_volume_change_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_watchlists", "last_volume_change_pct")
    op.drop_column("user_watchlists", "last_mcap_change_pct")
    op.drop_column("user_watchlists", "previous_volume")
    op.drop_column("user_watchlists", "previous_mcap")
    op.drop_column("user_watchlists", "initial_liquidity")
    op.drop_column("user_watchlists", "initial_volume")
    op.drop_column("user_watchlists", "initial_mcap")
