from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from database import (
    backfill_chain_identities_from_launches,
    close_db,
    create_or_update_token_outcome,
    db_session,
    get_chain_token_identity,
    get_latest_project_lore,
    init_db,
    list_bundle_signals,
    upsert_bundle_signal,
    upsert_agent_memory,
    upsert_chain_token_identity,
    upsert_launch,
    utc_now,
)
from services.agent_memory import build_memory_context, update_memory_from_outcome
from services.block_reader.bundle_detector import detect_bundle_like_patterns
from services.chains.types import NormalizedTx
from services.lore_extraction import build_lore_payload, extract_and_store_project_lore
from services.market_selection import select_canonical_market
from services.outcome_tracker import classify_outcome, initial_snapshot_from_outcome, next_due_window, record_outcome_snapshot
from services.verdict_v3 import build_output, build_verdict_input, build_verdict_v3


def ca(n: int) -> str:
    return "0x" + f"{n:040x}"


@pytest_asyncio.fixture()
async def db_url(tmp_path):
    path = tmp_path / "feature_foundation.db"
    await init_db(f"sqlite+aiosqlite:///{path}", auto_create=True)
    try:
        yield f"sqlite+aiosqlite:///{path}"
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_chain_identity_uses_chain_token_key(db_url):
    token = ca(201)
    async with db_session() as db:
        row = await upsert_chain_token_identity(
            db,
            chain="base",
            token_id=token,
            ticker="AI",
            name="AI Token",
            launch_source="dexscreener",
        )
        assert row.identity_key == f"base:{token}"

    async with db_session() as db:
        loaded = await get_chain_token_identity(db, chain="base", token_id=token)
        assert loaded is not None
        assert loaded.ticker == "AI"


def test_lore_ticker_only_stays_low_confidence():
    token = ca(202)
    payload = build_lore_payload(
        token_id=token,
        ticker="VEIL",
        name="Veil",
        social_evidence={
            "top_tweets": [
                {
                    "evidence_type": "ticker_strong",
                    "ticker_confirmed": True,
                    "username": "analyst",
                    "text": "$VEIL looks like privacy infrastructure with a real product angle.",
                    "url": "https://x.com/analyst/status/1",
                    "language": "en",
                }
            ]
        },
    )

    assert payload["ca_attribution_confidence"] == "LOW"
    assert payload["official_vs_unofficial"] == "ticker-context"
    assert "not safe to attribute" in payload["why_it_matters"]


@pytest.mark.asyncio
async def test_lore_primary_evidence_stores_project_lore(db_url):
    token = ca(203)
    async with db_session() as db:
        payload = await extract_and_store_project_lore(
            db,
            token_id=token,
            ticker="AGENT",
            name="Agent Protocol",
            social_evidence={
                "primary_tweets": [
                    {
                        "evidence_type": "ca_confirmed",
                        "ca_confirmed": True,
                        "username": "builder",
                        "text": f"{token} is an AI workflow automation platform for Base traders.",
                        "url": "https://x.com/builder/status/1",
                        "language": "en",
                    },
                    {
                        "evidence_type": "project_confirmed",
                        "username": "official",
                        "text": "Agent Protocol enables automation for trading workflows and analytics.",
                        "url": "https://x.com/official/status/2",
                        "language": "en",
                    },
                ]
            },
        )
        assert payload["ca_attribution_confidence"] == "MEDIUM"

    async with db_session() as db:
        row = await get_latest_project_lore(db, chain="base", token_id=token)
        assert row is not None
        assert row.attribution_confidence == "MEDIUM"


def test_verdict3_ticker_only_cannot_watch():
    token = ca(204)
    verdict_input = build_verdict_input(
        ca=token,
        launch={"symbol": "ONLY", "name": "Only"},
        research={
            "symbol": "ONLY",
            "market": {"mcap": 800_000, "volume_24h": 700_000, "liquidity": 250_000},
            "social": {
                "social_score": 95,
                "source_provenance": {"ticker_strong": 20},
            },
            "project_lore": {
                "narrative_summary": "Ticker-only AI platform narrative.",
                "why_it_matters": "Ticker context exists but attribution is weak.",
                "ca_attribution_confidence": "LOW",
                "project_category": "AI / Agent",
            },
            "project_narrative": {"same_ticker_collision": True},
            "onchain": {"provider": "stub"},
        },
    )
    output = build_output(verdict_input)

    assert output["label"] == "SKIP"
    assert output["score"] < 75


def test_outcome_classifier_detects_winner_and_dump():
    winner, _ = classify_outcome({"mcap": 100_000, "liquidity": 50_000}, {"mcap": 550_000, "liquidity": 55_000})
    dumped, _ = classify_outcome({"mcap": 100_000, "liquidity": 50_000}, {"mcap": 10_000, "liquidity": 45_000})

    assert winner == "winner_24h"
    assert dumped == "dumped"


@pytest.mark.asyncio
async def test_agent_memory_does_not_need_raw_tweet_body(db_url):
    async with db_session() as db:
        memory = await upsert_agent_memory(
            db,
            memory_key="base:dev_wallet:0xabc",
            memory_type="dev_wallet_repeated_failure",
            subject_type="dev_wallet",
            subject_id="base:0xabc",
            insight="Dev wallet has repeated failed launches.",
            normalized_json={"score_adjustment": -6, "evidence": ["0x1", "0x2", "0x3"]},
            polarity="negative",
            confidence=0.8,
            evidence_count=3,
        )

    assert "raw_tweet" not in memory.normalized_json
    assert memory.confidence == 0.8


@pytest.mark.asyncio
async def test_token_outcome_upsert_is_idempotent(db_url):
    token = ca(205)
    async with db_session() as db:
        first = await create_or_update_token_outcome(
            db,
            chain="base",
            token_id=token,
            ticker="OUT",
            first_seen_at=utc_now(),
        )
        second = await create_or_update_token_outcome(
            db,
            chain="base",
            token_id=token,
            ticker="OUT",
            first_seen_at=utc_now(),
        )

    assert first.id == second.id


@pytest.mark.asyncio
async def test_verdict3_persists_shadow_output(db_url, monkeypatch):
    import services.verdict_v3 as verdict_v3

    token = ca(206)
    research = {
        "symbol": "V3",
        "market": {"mcap": 120_000, "volume_24h": 80_000, "liquidity": 60_000},
        "social": {"source_provenance": {"ca_confirmed": 3}, "social_score": 60},
        "project_lore": {
            "narrative_summary": "V3 is a compact trading analytics tool.",
            "why_it_matters": "It gives traders a clearer workflow.",
            "ca_attribution_confidence": "MEDIUM",
            "project_category": "Trading / Tooling",
        },
        "onchain": {"provider": "stub"},
    }

    async def fake_latest_research(db, ca):
        return SimpleNamespace(processed_data=research)

    monkeypatch.setattr(verdict_v3, "get_latest_token_research", fake_latest_research)

    async with db_session() as db:
        output = await build_verdict_v3(db, ca=token, launch={"symbol": "V3", "name": "V3"})

    assert output["id"]
    assert output["label"] in {"WAIT", "SKIP", "WATCH", "HIGH RISK"}
    assert "Verdict 3.0" in output["human_readable"]


@pytest.mark.asyncio
async def test_backfill_launches_into_chain_identity(db_url):
    token = ca(207)
    async with db_session() as db:
        await upsert_launch(
            db,
            ca=token,
            source="bankr",
            ticker="BACK",
            name="Backfill",
            launched_at=utc_now(),
            raw_json={"x_username": "builder"},
        )
        count = await backfill_chain_identities_from_launches(db, limit=10)
        assert count == 1

    async with db_session() as db:
        row = await get_chain_token_identity(db, chain="base", token_id=token)
        assert row is not None
        assert row.launch_source == "bankr"


def test_canonical_market_selector_rejects_wrong_side_common_asset():
    token = ca(209)
    selected = select_canonical_market(
        [
            {"base_token_address": ca(1), "base_token_symbol": "WETH", "quote_token_symbol": "TEST", "liquidity": 1_000_000, "volume_24h": 1_000_000},
            {"base_token_address": token, "base_token_symbol": "TEST", "quote_token_symbol": "WETH", "liquidity": 50_000, "volume_24h": 60_000},
        ],
        token_id=token,
        ticker="TEST",
    )
    assert selected is not None
    assert selected["base_token_address"] == token
    assert selected["canonical_pool_confidence"] == "HIGH"


def test_bundle_detector_flags_same_block_cluster_without_claiming_certainty():
    token = ca(210)
    transfers = [
        NormalizedTx(
            chain="base",
            tx_hash=f"0x{i:064x}",
            block_number=100,
            tx_index=i,
            timestamp=None,
            from_address=ca(300),
            to_address=ca(400 + i),
            event_type="transfer",
            token_id=token,
            wallet_address=ca(400 + i),
            pair_address=None,
            amount_token=100,
            amount_native=None,
            raw={},
        )
        for i in range(5)
    ]
    summary = detect_bundle_like_patterns(transfers)
    assert summary.bundle_risk >= 25
    assert summary.confidence == "LOW"
    assert summary.signals[0]["type"] == "same_block_buy_cluster"


@pytest.mark.asyncio
async def test_bundle_signal_persistence_is_idempotent(db_url):
    token = ca(211)
    async with db_session() as db:
        first = await upsert_bundle_signal(
            db,
            chain="base",
            token_id=token,
            signal_type="same_block_buy_cluster",
            severity="MEDIUM",
            risk_score=25,
            score_impact=-6,
            title="Same-block buyer cluster",
        )
        second = await upsert_bundle_signal(
            db,
            chain="base",
            token_id=token,
            signal_type="same_block_buy_cluster",
            severity="HIGH",
            risk_score=80,
            score_impact=-15,
            title="Updated cluster",
        )
        rows = await list_bundle_signals(db, chain="base", token_id=token)

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].risk_score == 80


@pytest.mark.asyncio
async def test_memory_update_from_completed_outcomes_is_bounded(db_url):
    wallet = ca(212)
    async with db_session() as db:
        labels = ["dumped", "dead", "dumped", "dead", "winner_24h", "flat", "dumped"]
        for idx, label in enumerate(labels, start=1):
            await create_or_update_token_outcome(
                db,
                chain="base",
                token_id=ca(220 + idx),
                ticker="MEM",
                deployer_wallet=wallet,
                first_seen_at=utc_now(),
                final_outcome_label=label,
                status="completed",
                snapshot_key="7d",
                snapshot_json={"market": {"mcap": 1}},
            )
        outcome = await create_or_update_token_outcome(
            db,
            chain="base",
            token_id=ca(224),
            ticker="MEM",
            deployer_wallet=wallet,
            first_seen_at=utc_now(),
            final_outcome_label="dumped",
            status="completed",
            snapshot_key="7d",
            snapshot_json={"market": {"mcap": 1}},
        )
        keys = await update_memory_from_outcome(db, outcome)
        context = await build_memory_context(db, chain="base", deployer_wallet=wallet, ticker="MEM")

    assert keys
    assert -12 <= context["score_adjustment"] <= 8
    assert context["confidence"] >= 0.3


@pytest.mark.asyncio
async def test_outcome_window_progression_and_final_memory_update(db_url):
    token = ca(230)
    async with db_session() as db:
        outcome = await create_or_update_token_outcome(
            db,
            chain="base",
            token_id=token,
            ticker="WIN",
            first_seen_at=utc_now(),
            snapshot_key="1h",
            snapshot_json={"initial": {"mcap": 100_000, "liquidity": 50_000}},
        )
        assert next_due_window(outcome) == "1h"
        assert initial_snapshot_from_outcome(outcome)["mcap"] == 100_000
        result = await record_outcome_snapshot(
            db,
            chain="base",
            token_id=token,
            window="7d",
            dex={"mcap": 700_000, "liquidity": 55_000, "volume_24h": 100_000},
            initial_snapshot={"mcap": 100_000, "liquidity": 50_000},
        )

    assert result["label"] == "winner_24h"


@pytest.mark.asyncio
async def test_verdict3_uses_persisted_block_reader_risk(db_url, monkeypatch):
    import services.verdict_v3 as verdict_v3

    token = ca(240)
    research = {
        "symbol": "RISK",
        "market": {"mcap": 500_000, "volume_24h": 300_000, "liquidity": 150_000},
        "social": {"source_provenance": {"ca_confirmed": 5}, "social_score": 80},
        "project_lore": {
            "narrative_summary": "Risk is a Base analytics protocol.",
            "why_it_matters": "It gives traders better tooling.",
            "ca_attribution_confidence": "HIGH",
            "project_category": "Trading / Tooling",
        },
    }

    async def fake_latest_research(db, ca):
        return SimpleNamespace(processed_data=dict(research))

    monkeypatch.setattr(verdict_v3, "get_latest_token_research", fake_latest_research)
    async with db_session() as db:
        await upsert_bundle_signal(
            db,
            chain="base",
            token_id=token,
            signal_type="same_block_buy_cluster",
            severity="HIGH",
            risk_score=85,
            score_impact=-15,
            title="Same-block buyer cluster",
        )
        output = await build_verdict_v3(db, ca=token, launch={"symbol": "RISK", "source": "bankr"})

    assert output["label"] == "HIGH RISK"
    assert output["onchain_risk"]["bundle_risk"] == 85
