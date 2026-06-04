from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from database import (
    close_db,
    create_or_update_token_outcome,
    db_session,
    get_chain_token_identity,
    get_latest_project_lore,
    init_db,
    upsert_agent_memory,
    upsert_chain_token_identity,
    utc_now,
)
from services.lore_extraction import build_lore_payload, extract_and_store_project_lore
from services.outcome_tracker import classify_outcome
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
