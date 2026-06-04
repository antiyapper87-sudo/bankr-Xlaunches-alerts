from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from database import (
    close_db,
    db_session,
    deactivate_watchlist_item,
    deactivate_tracked_wallet,
    get_due_watchlist_items,
    get_due_tracked_wallets,
    get_due_delivery_retries,
    get_due_rechecks,
    get_tenant_settings,
    get_status_snapshot,
    init_db,
    list_tracked_wallets,
    list_watchlist_items,
    mark_delivery_retry,
    mark_delivery_sending,
    mark_launch_status,
    mark_tracked_wallet_checked,
    mark_watchlist_checked,
    provider_available,
    queue_recheck,
    set_provider_cooldown,
    update_tenant_min_score,
    upsert_tracked_wallet,
    upsert_user_feedback,
    upsert_launch,
    upsert_wallet_event,
    upsert_watchlist_item,
    upsert_tenant,
    utc_now,
)
from services.delivery import prepare_signal_fanout, prepare_tenant_delivery
from services.project_narrative import extract_project_narrative
from services.research_pipeline import run_research_pipeline
from services import spoof_detector
from services.spoof_detector import detect_spoof_signals
from services.tenants import ensure_telegram_tenant
from services.verdict_v2 import build_verdict_v2


def ca(n: int) -> str:
    return "0x" + f"{n:040x}"


@pytest_asyncio.fixture()
async def db_url(tmp_path):
    path = tmp_path / "phase1.db"
    await init_db(f"sqlite+aiosqlite:///{path}", auto_create=True)
    try:
        yield f"sqlite+aiosqlite:///{path}"
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_launch_dedupe_and_recheck_survive_restart(db_url):
    token_ca = ca(1)
    async with db_session() as db:
        _, inserted = await upsert_launch(
            db,
            ca=token_ca,
            ticker="ONE",
            name="One",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "ONE", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        assert inserted is True
        _, inserted_again = await upsert_launch(
            db,
            ca=token_ca,
            ticker="ONE",
            name="One",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "ONE", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        assert inserted_again is False

        recheck_ca = ca(2)
        await upsert_launch(
            db,
            ca=recheck_ca,
            ticker="TWO",
            name="Two",
            source="clanker",
            raw_json={"address": recheck_ca, "symbol": "TWO", "source": "clanker"},
            launched_at=utc_now(),
            status="new",
        )
        await queue_recheck(
            db,
            ca=recheck_ca,
            reason="no market data",
            next_check_at=utc_now() - timedelta(seconds=1),
            no_data=True,
        )

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        due = await get_due_rechecks(db, now=utc_now(), limit=10)
        assert [row.ca for row in due] == [recheck_ca]


@pytest.mark.asyncio
async def test_fanout_is_idempotent_for_1000_tenants(db_url):
    token_ca = ca(3)
    async with db_session() as db:
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="FAN",
            name="Fanout",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "FAN", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        for idx in range(1000):
            await upsert_tenant(db, tenant_type="telegram_user", external_id=str(10_000 + idx), title=f"user-{idx}")

        _, inserted = await prepare_signal_fanout(
            db,
            ca=token_ca,
            source="bankr",
            verdict_score=8.0,
            verdict_label="watch",
            payload_json={"telegram_text": "signal", "reply_markup": {}},
        )
        assert inserted == 1000

        _, inserted_again = await prepare_signal_fanout(
            db,
            ca=token_ca,
            source="bankr",
            verdict_score=8.0,
            verdict_label="watch",
            payload_json={"telegram_text": "signal", "reply_markup": {}},
        )
        assert inserted_again == 0

        status = await get_status_snapshot(db)
        assert status["tenants_active"] == 1000
        assert status["signals_total"] == 1
        assert status["deliveries_pending"] == 1000


@pytest.mark.asyncio
async def test_public_start_tenant_receives_all_default_signal_sources(db_url):
    token_ca = ca(30)
    async with db_session() as db:
        tenant = await ensure_telegram_tenant(db, "544999608", title="public user")
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="CG",
            name="CoinGecko Source",
            source="coingecko",
            raw_json={"address": token_ca, "symbol": "CG", "source": "coingecko"},
            launched_at=utc_now(),
            status="new",
        )

        _, inserted = await prepare_signal_fanout(
            db,
            ca=token_ca,
            source="coingecko",
            verdict_score=6.0,
            verdict_label="wait",
            payload_json={"telegram_text": "signal", "reply_markup": {}},
        )

        assert tenant.external_id == "544999608"
        assert inserted == 1


@pytest.mark.asyncio
async def test_phase3_watchlist_settings_and_feedback_survive_restart(db_url):
    token_ca = ca(31)
    async with db_session() as db:
        tenant = await ensure_telegram_tenant(db, "700001", title="retention user")
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="RET3",
            name="Retention",
            source="dexscreener",
            raw_json={"address": token_ca, "symbol": "RET3", "source": "dexscreener"},
            launched_at=utc_now(),
            status="signaled",
        )
        settings = await update_tenant_min_score(db, tenant_id=tenant.id, min_score=7.5)
        item, inserted = await upsert_watchlist_item(
            db,
            tenant_id=tenant.id,
            ca=token_ca,
            label="core",
            market_json={"mcap": 100_000, "volume_24h": 50_000, "liquidity": 30_000, "price_usd": "0.01"},
        )
        feedback, feedback_inserted = await upsert_user_feedback(
            db,
            tenant_id=tenant.id,
            ca=token_ca,
            action="worth_watching",
            payload_json={"source": "test"},
        )
        assert settings.min_score == 7.5
        assert inserted is True
        assert item.status == "active"
        assert feedback.action == "worth_watching"
        assert feedback_inserted is True

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        tenant = await ensure_telegram_tenant(db, "700001", title="retention user")
        settings = await get_tenant_settings(db, tenant_id=tenant.id)
        items = await list_watchlist_items(db, tenant_id=tenant.id)
        assert settings.min_score == 7.5
        assert len(items) == 1
        assert items[0].label == "core"
        due = await get_due_watchlist_items(db, now=utc_now() + timedelta(minutes=20), limit=10, min_interval_seconds=900)
        assert [row.ca for row in due] == [token_ca]
        await mark_watchlist_checked(
            db,
            watchlist_id=items[0].id,
            market_json={"mcap": 200_000, "volume_24h": 120_000, "liquidity": 55_000, "price_usd": "0.02"},
            notified=True,
        )
        assert await deactivate_watchlist_item(db, tenant_id=tenant.id, ca=token_ca) is True
        assert await list_watchlist_items(db, tenant_id=tenant.id) == []


@pytest.mark.asyncio
async def test_phase4_tracked_wallet_and_event_ledger_are_persistent_and_idempotent(db_url):
    token_ca = ca(32)
    wallet_address = ca(100)
    async with db_session() as db:
        tenant = await ensure_telegram_tenant(db, "700002", title="wallet user")
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="WAL",
            name="Wallet Token",
            source="dexscreener",
            raw_json={"address": token_ca, "symbol": "WAL", "source": "dexscreener"},
            launched_at=utc_now(),
            status="signaled",
        )
        wallet, inserted = await upsert_tracked_wallet(db, tenant_id=tenant.id, address=wallet_address, label="alpha")
        event, event_inserted = await upsert_wallet_event(
            db,
            tracked_wallet_id=wallet.id,
            tenant_id=tenant.id,
            wallet_address=wallet_address,
            ca=token_ca,
            direction="in",
            tx_hash="0xabc",
            block_number=123,
            amount=10.0,
            event_json={"source": "test"},
        )
        _, duplicate_inserted = await upsert_wallet_event(
            db,
            tracked_wallet_id=wallet.id,
            tenant_id=tenant.id,
            wallet_address=wallet_address,
            ca=token_ca,
            direction="in",
            tx_hash="0xabc",
            block_number=123,
            amount=10.0,
            event_json={"source": "test"},
        )
        assert inserted is True
        assert event.ca == token_ca
        assert event_inserted is True
        assert duplicate_inserted is False
        research = await run_research_pipeline(
            db,
            ca=token_ca,
            dex={"mcap": 100_000, "volume_24h": 80_000, "liquidity": 50_000, "price_usd": "0.01"},
            requested_by="test_wallet",
        )
        assert research["processed_data"]["smart_money"]["inflow_wallets"] == 1

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        tenant = await ensure_telegram_tenant(db, "700002", title="wallet user")
        wallets = await list_tracked_wallets(db, tenant_id=tenant.id)
        assert len(wallets) == 1
        assert wallets[0].address == wallet_address
        due = await get_due_tracked_wallets(db, now=utc_now() + timedelta(minutes=2), limit=10, min_interval_seconds=60)
        assert [row.address for row in due] == [wallet_address]
        await mark_tracked_wallet_checked(db, wallet_id=wallets[0].id, block_number=456)
        assert await deactivate_tracked_wallet(db, tenant_id=tenant.id, address=wallet_address) is True
        assert await list_tracked_wallets(db, tenant_id=tenant.id) == []


@pytest.mark.asyncio
async def test_delivery_retry_ledger_survives_restart(db_url):
    token_ca = ca(4)
    async with db_session() as db:
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="RET",
            name="Retry",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "RET", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        tenant = await upsert_tenant(db, tenant_type="telegram_user", external_id="544999608", title="dm")
        _, delivery, _ = await prepare_tenant_delivery(
            db,
            ca=token_ca,
            tenant_id=tenant.id,
            chat_id="544999608",
            payload_json={"telegram_text": "retry me", "reply_markup": {}},
        )
        await mark_delivery_sending(db, delivery_id=delivery.id)
        await mark_delivery_retry(
            db,
            delivery_id=delivery.id,
            error="telegram send failed",
            next_retry_at=utc_now() - timedelta(seconds=1),
        )

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        due = await get_due_delivery_retries(db, now=utc_now(), limit=10)
        assert len(due) == 1
        assert due[0].payload_json["telegram_text"] == "retry me"


def test_signal_format_stays_under_telegram_limit():
    from main import format_signal_telegram

    token_ca = ca(5)
    text = format_signal_telegram(
        {"address": token_ca, "symbol": "FMT", "name": "Format", "source": "bankr"},
        {
            "mcap": 50_000,
            "volume_24h": 25_000,
            "liquidity": 10_000,
            "price_usd": "0.001",
            "price_change_1h": 12.3,
            "pair_url": f"https://dexscreener.com/base/{token_ca}",
        },
    )
    assert len(text) < 4096
    assert "/research" not in text


def test_signal_keyboard_is_research_only(monkeypatch):
    import main

    monkeypatch.setattr(main, "FOMO_ENABLED", False)

    keyboard = main.build_signal_keyboard(ca(6), "FMT")
    labels = [
        button["text"]
        for row in keyboard["inline_keyboard"]
        for button in row
    ]

    assert labels == ["🔎 X Research", "⭐ Worth watching", "📤 Share ticker"]
    assert all("Buy" not in label and "Banana" not in label for label in labels)
    share_url = next(
        button["url"]
        for row in keyboard["inline_keyboard"]
        for button in row
        if button["text"] == "📤 Share ticker"
    )
    assert "%24FMT" in share_url


def test_signal_keyboard_includes_fomo_when_enabled(monkeypatch):
    import main

    monkeypatch.setattr(main, "FOMO_ENABLED", True)
    monkeypatch.setattr(main, "FOMO_DEFAULT_CHAIN_ID", 8453)

    token_ca = ca(7)
    keyboard = main.build_signal_keyboard(token_ca, "FMT")
    labels = [
        button["text"]
        for row in keyboard["inline_keyboard"]
        for button in row
    ]

    assert "👀 Fomo" in labels
    assert "🚀 Deep Research" in labels
    fomo_url = next(
        button["url"]
        for row in keyboard["inline_keyboard"]
        for button in row
        if button["text"] == "👀 Fomo"
    )
    assert fomo_url == f"https://fomo.family/coin?address={token_ca}&chainId=8453"


def test_watchlist_message_is_grouped_and_actionable(monkeypatch):
    import main

    monkeypatch.setattr(main, "FOMO_ENABLED", False)
    now = utc_now()
    hot = SimpleNamespace(
        id=1,
        ca=ca(41),
        label="alpha",
        last_mcap=180_000,
        last_volume=95_000,
        last_liquidity=42_000,
        initial_mcap=100_000,
        initial_volume=50_000,
        last_mcap_change_pct=42.0,
        last_volume_change_pct=10.0,
        last_market_json={"token_symbol": "HOT", "token_name": "Hot Token"},
        created_at=now - timedelta(hours=30),
        last_checked_at=now - timedelta(minutes=12),
    )
    recent = SimpleNamespace(
        id=2,
        ca=ca(42),
        label=None,
        last_mcap=70_000,
        last_volume=31_000,
        last_liquidity=22_000,
        initial_mcap=70_000,
        initial_volume=31_000,
        last_mcap_change_pct=None,
        last_volume_change_pct=None,
        last_market_json={"token_symbol": "NEW", "token_name": "New Token"},
        created_at=now - timedelta(hours=2),
        last_checked_at=now - timedelta(minutes=5),
    )

    message = main.build_watchlist_message([recent, hot], launches={})
    keyboard = main.build_watchlist_keyboard([recent, hot], launches={})

    assert "🔥 Hot Movers" in message
    assert "🆕 Recently Added" in message
    assert "$HOT" in message
    assert ca(41) not in message
    assert keyboard and any(button["callback_data"].startswith("wl_research:") for row in keyboard["inline_keyboard"] for button in row if "callback_data" in button)


def test_watch_symbol_name_accepts_launch_dict():
    import main

    item = SimpleNamespace(
        ca=ca(43),
        label=None,
        last_market_json={},
    )

    assert main.watch_symbol_name(item, {"symbol": "VEIL", "name": "Veil Token"}) == ("VEIL", "Veil Token")


def test_watch_ticker_search_prefers_exact_base_symbol():
    import main

    exact = {
        "attributes": {"name": "VEIL / WETH", "volume_usd": {"h24": "1000"}, "reserve_in_usd": "10000"},
        "relationships": {"base_token": {"data": {"id": f"base_{ca(44)}"}}},
    }
    noisy = {
        "attributes": {"name": "NOTVEIL / WETH", "volume_usd": {"h24": "999999"}, "reserve_in_usd": "999999"},
        "relationships": {"base_token": {"data": {"id": f"base_{ca(45)}"}}},
    }

    assert main.choose_gecko_search_pool([noisy, exact], "$veil") is exact


def gecko_pool(symbol: str, token_ca: str, *, mcap: int, volume: int, liquidity: int) -> dict:
    return {
        "attributes": {
            "name": f"{symbol} / WETH",
            "market_cap_usd": str(mcap),
            "volume_usd": {"h24": str(volume)},
            "reserve_in_usd": str(liquidity),
        },
        "relationships": {"base_token": {"data": {"id": f"base_{token_ca}"}}},
    }


def test_watch_ticker_candidates_require_market_filters():
    import main

    weak = gecko_pool("VEIL", ca(46), mcap=80_000, volume=70_000, liquidity=10_000)
    strong = gecko_pool("VEIL", ca(47), mcap=80_000, volume=70_000, liquidity=70_000)

    assert main.pool_passes_watch_filters(weak) is False
    assert main.pool_passes_watch_filters(strong) is True
    assert main.choose_gecko_search_pool([weak, strong], "$veil") is strong


def test_watch_ambiguous_message_lists_ca_candidates():
    import main

    candidates = [
        {"address": ca(48), "symbol": "VEIL", "name": "Veil One", "mcap": 90_000, "volume_24h": 80_000, "liquidity": 70_000},
        {"address": ca(49), "symbol": "VEIL", "name": "Veil Two", "mcap": 120_000, "volume_24h": 90_000, "liquidity": 75_000},
    ]

    message = main.format_watch_ambiguous_message("$veil", candidates)

    assert "Multiple Base tokens" in message
    assert ca(48) in message
    assert ca(49) in message
    assert "/watch 0x..." in message


def socialdata_tweet(
    *,
    tweet_id: int,
    text: str,
    views: int = 100,
    likes: int = 10,
    username: str = "researcher",
    created_at: str = "2026-06-04T10:00:00Z",
) -> dict:
    return {
        "id_str": str(tweet_id),
        "full_text": text,
        "favorite_count": likes,
        "retweet_count": 1,
        "reply_count": 1,
        "views_count": views,
        "bookmark_count": 0,
        "quote_count": 0,
        "created_at": created_at,
        "user": {
            "screen_name": username,
            "name": username,
            "followers_count": 2_500,
        },
    }


def test_socialdata_hard_spam_url_is_removed_before_scoring():
    from main import parse_socialdata_tweet

    item = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=10,
            text="5m Buy $OPTICODE 0xabc Check https://OKAI.HK/ALpha",
            views=1_000,
            likes=100,
        )
    )
    assert item is None


def test_socialdata_engagement_gate_requires_views_and_likes():
    from main import parse_socialdata_tweet

    assert parse_socialdata_tweet(
        socialdata_tweet(tweet_id=11, text="Strong thesis on $CUE volume and holders", views=49, likes=10)
    ) is None
    assert parse_socialdata_tweet(
        socialdata_tweet(tweet_id=12, text="Strong thesis on $CUE volume and holders", views=100, likes=4)
    ) is None
    assert parse_socialdata_tweet(
        socialdata_tweet(tweet_id=13, text="Strong thesis on $CUE volume and holders", views=50, likes=5)
    ) is not None


def test_research_filter_requires_five_qualified_tweets():
    from main import filter_research_tweets, parse_socialdata_tweet

    phrases = [
        "$CUE thesis undervalued volume holders onchain catalyst",
        "$CUE whale accumulation confirms early market inflows",
        "$CUE protocol revenue and liquidity trend look mispriced",
        "$CUE watchlist entry after strong holder growth and volume",
        "$CUE asymmetric Base play with fees and mainnet traction",
    ]
    raw = [
        socialdata_tweet(
            tweet_id=i,
            username=f"researcher{i}",
            text=phrases[i - 20],
            views=500,
            likes=20,
        )
        for i in range(20, 25)
    ]
    four = [parse_socialdata_tweet(tweet) for tweet in raw[:4]]
    five = [parse_socialdata_tweet(tweet) for tweet in raw]

    assert filter_research_tweets(four, ticker="CUE", limit=6, allow_tier3=True) == []
    selected = filter_research_tweets(five, ticker="CUE", limit=6, allow_tier3=True)
    assert len(selected) == 5


def test_research_view_uses_24h_window_and_hides_contract_in_x_signal():
    from main import filter_research_tweets, format_research_social_block, parse_socialdata_tweet
    from services.social_evidence import build_social_evidence

    token_ca = ca(64)
    recent_dt = (utc_now() - timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    old_dt = (utc_now() - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    phrases = [
        "utility platform traction holders with data workflow narrative",
        "AI agent tooling with real users, app layer and volume context",
        "developer ecosystem angle, automation API and Base-native product",
        "community is discussing mainnet use, revenue path and product hook",
        "scanner terminal narrative with onchain utility and strong retention",
    ]
    recent = [
        parse_socialdata_tweet(
            socialdata_tweet(
                tweet_id=90 + i,
                username=f"research{i}",
                text=f"{token_ca} $KTA {phrases[i]}",
                views=2_000,
                likes=20,
                created_at=recent_dt,
            )
        )
        for i in range(len(phrases))
    ]
    old = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=99,
            text=f"{token_ca} $KTA utility platform traction holders old",
            views=2_000,
            likes=20,
            created_at=old_dt,
        )
    )

    selected = filter_research_tweets(
        recent + [old],
        ticker="KTA",
        address=token_ca,
        max_age_hours=24,
        allow_tier3=True,
    )
    assert len(selected) == 5
    evidence = build_social_evidence(selected, ticker="KTA", address=token_ca, max_age_hours=24)
    block = format_research_social_block("KTA", selected, [], social_evidence=evidence, address=token_ca)

    assert token_ca not in block
    assert "X signal" in block
    assert "Thesis:" in block
    assert "Why it matters:" not in block
    assert "Social Score:" not in block
    assert "❤️ 20" in block
    assert "👁 2K" in block
    assert "🔄 1" in block
    assert "S20" not in block
    assert "2Kf" not in block


def test_research_x_signal_keeps_thesis_two_sentences_and_hides_non_english():
    from main import format_research_social_block
    from services.social_evidence import build_social_evidence

    token_ca = ca(66)
    english = {
        "username": "english_research",
        "url": "https://x.com/english_research/status/1",
        "text": (
            f"Contract Address: {token_ca} $ENG has a real utility angle for Base automation. "
            "The second sentence explains why the market is paying attention. "
            "The third sentence should not be required."
        ),
        "followers": 15_000,
        "views": 3_200,
        "likes": 42,
        "retweets": 7,
        "replies": 3,
        "score": 12,
        "thesis_quality": 7,
        "created_at": utc_now(),
    }
    evidence = build_social_evidence([english], ticker="ENG", address=token_ca, min_count=1)
    block = format_research_social_block("ENG", [], [], social_evidence=evidence, address=token_ca)

    assert "<b>Thesis:</b>" in block
    assert token_ca not in block
    assert "Contract Address" not in block
    assert "real utility angle" in block
    assert "second sentence explains" in block
    assert "third sentence should not" not in block
    assert "Social Score:" not in block


def test_research_x_signal_rejects_mixed_language_tweets():
    from main import format_research_social_block
    from services.social_evidence import build_social_evidence

    token_ca = ca(67)
    tweet = {
        "username": "mixed_research",
        "url": "https://x.com/mixed_research/status/1",
        "text": (
            f"{token_ca} $MIX has a useful Base app narrative with real automation demand. "
            "Second sentence keeps the concrete product angle for the card. "
            "核心理念很强，势头很好，应该不会出现在输出里。"
        ),
        "followers": 22_000,
        "views": 8_100,
        "likes": 91,
        "retweets": 14,
        "replies": 5,
        "score": 15,
        "thesis_quality": 8,
        "created_at": utc_now(),
    }

    evidence = build_social_evidence([tweet], ticker="MIX", address=token_ca, min_count=1)
    block = format_research_social_block("MIX", [], [], social_evidence=evidence, address=token_ca)

    assert "No CA-verified qualified tweets" in block
    assert token_ca not in block
    assert "useful Base app narrative" not in block
    assert "核心理念" not in block


def test_hermes_filters_blocked_accounts_and_shill_patterns():
    from main import filter_research_tweets, parse_socialdata_tweet

    token_ca = ca(68)
    blocked = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=130,
            username="bankrbot",
            text=f"{token_ca} $BAD has a useful Base app narrative with real automation demand.",
            views=10_000,
            likes=100,
        )
    )
    shill = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=131,
            username="real_user",
            text=f"{token_ca} $BAD Next 100x moon soon, buy on exchange now.",
            views=10_000,
            likes=100,
        )
    )
    non_english = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=132,
            username="real_user2",
            text=f"{token_ca} $BAD 这是一个中文推文，不应该出现在输出里。",
            views=10_000,
            likes=100,
        )
    )

    assert blocked is None
    assert shill is None
    assert non_english is None
    assert filter_research_tweets(
        [item for item in [blocked, shill, non_english] if item],
        ticker="BAD",
        address=token_ca,
        allow_tier3=True,
        min_count=0,
    ) == []


@pytest.mark.asyncio
async def test_smart_fetch_calls_socialdata_only_when_nitter_alpha():
    from services.social_fetcher import AlphaDetector, NitterFetcher, SmartFetchOrchestrator, SocialDataFetcher

    calls = {"top": 0}

    async def latest_no_alpha(**kwargs):
        return [
            {
                "text": "$NOPE gm",
                "views": 60,
                "likes": 5,
                "retweets": 0,
                "replies": 0,
                "followers": 100,
                "thesis_quality": 0,
            }
        ]

    async def latest_alpha(**kwargs):
        return [
            {
                "text": "$ALPHA has a real utility thesis with product traction and workflow context.",
                "views": 2_000,
                "likes": 30,
                "retweets": 4,
                "replies": 3,
                "followers": 8_000,
                "thesis_quality": 7,
            }
        ]

    async def top(**kwargs):
        calls["top"] += 1
        return [{"text": "$ALPHA top tweet"}]

    no_alpha = SmartFetchOrchestrator(
        nitter=NitterFetcher(latest_no_alpha),
        socialdata=SocialDataFetcher(top),
        alpha_detector=AlphaDetector(),
    )
    result = await no_alpha.fetch(ticker="NOPE")
    assert result.socialdata_called is False
    assert calls["top"] == 0

    with_alpha = SmartFetchOrchestrator(
        nitter=NitterFetcher(latest_alpha),
        socialdata=SocialDataFetcher(top),
        alpha_detector=AlphaDetector(),
    )
    result = await with_alpha.fetch(ticker="ALPHA")
    assert result.socialdata_called is True
    assert calls["top"] == 1


def test_research_card_accepts_nitter_mentions_for_base_ca():
    from main import format_research_card, parse_socialdata_tweet

    token_ca = ca(65)
    tweet = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=120,
            username="nitter_source",
            text=f"{token_ca} $BASE has utility platform traction and narrative",
            views=1_500,
            likes=15,
        )
    )
    body = format_research_card(
        token_name="Base Token",
        ticker="BASE",
        address=token_ca,
        dex={"mcap": 100_000, "volume_24h": 50_000, "liquidity": 40_000, "price_change_1h": 3.2},
        deployer_info=None,
        x_mentions=[],
        influencer_mentions=[],
        nitter_mentions=[tweet],
        social_evidence={},
        launch_status=None,
    )

    assert "Research" in body
    assert "nitter_source" in body
    tweet_lines = [line for line in body.splitlines() if "utility platform" in line]
    assert tweet_lines and all(token_ca not in line for line in tweet_lines)
    assert "❤️ 15" in body
    assert "👁 2K" in body


def test_hermes_social_evidence_builds_thesis_and_ranked_refs():
    from main import parse_socialdata_tweet
    from services.social_evidence import build_social_evidence

    token_ca = ca(63)
    raw = [
        socialdata_tweet(
            tweet_id=70 + i,
            username=f"builder{i}",
            text=f"{token_ca} $UTIL is an AI agent framework with API, automation, data workflow and mainnet traction {i}",
            views=2_000 + i * 500,
            likes=20 + i * 5,
        )
        for i in range(5)
    ]
    tweets = [parse_socialdata_tweet(tweet) for tweet in raw]

    evidence = build_social_evidence(tweets, ticker="UTIL", address=token_ca, min_count=5)

    assert evidence["qualified"] is True
    assert evidence["qualified_tweets"] == 5
    assert evidence["project_value"] == "Utility / Tech"
    assert "utility/tech" in evidence["thesis"].lower()
    assert evidence["agent"]["rules_loaded"] is True
    assert evidence["agent"]["rule_files"] == ["identity.md", "agents.md", "memory.md"]
    assert [item["ref"] for item in evidence["top_tweets"][:3]] == [1, 2, 3]
    assert evidence["top_tweets"][0]["score"] >= evidence["top_tweets"][-1]["score"]


def test_hermes_tier_score_sorts_evidence_before_legacy_score():
    from services.social_evidence import build_social_evidence

    token_ca = ca(69)
    long_high_engagement = {
        "username": "top_signal",
        "url": "https://x.com/top_signal/status/1",
        "text": f"{token_ca} $TIER " + ("This is a detailed utility thesis with product context and market traction. " * 6),
        "followers": 2_000,
        "views": 20_000,
        "likes": 200,
        "retweets": 40,
        "replies": 20,
        "score": 5,
        "thesis_quality": 7,
        "created_at": utc_now(),
    }
    short_legacy_strong = {
        "username": "legacy_strong",
        "url": "https://x.com/legacy_strong/status/2",
        "text": f"{token_ca} $TIER utility thesis traction",
        "followers": 100_000,
        "views": 800,
        "likes": 10,
        "retweets": 1,
        "replies": 1,
        "score": 25,
        "thesis_quality": 5,
        "created_at": utc_now(),
    }

    evidence = build_social_evidence(
        [short_legacy_strong, long_high_engagement],
        ticker="TIER",
        address=token_ca,
        min_count=1,
    )

    assert evidence["top_tweets"][0]["username"] == "top_signal"
    assert evidence["top_tweets"][0]["tweet_tier"] in {"S", "A", "B"}
    assert evidence["top_tweets"][0]["tweet_tier_score"] > evidence["top_tweets"][1]["tweet_tier_score"]


def test_xsignal_paginates_when_more_than_eight_tweets():
    from main import build_xsignal_pagination_keyboard, format_research_social_block
    from services.social_evidence import build_social_evidence

    token_ca = ca(70)
    tweets = []
    for idx in range(14):
        tweets.append(
            {
                "username": f"page_user{idx}",
                "url": f"https://x.com/page_user{idx}/status/{idx}",
                "text": (
                    f"{token_ca} $PAGE has a useful Base product thesis with automation, "
                    f"workflow context and market traction signal number {idx}."
                ),
                "followers": 10_000 + idx,
                "views": 2_000 + idx * 100,
                "likes": 20 + idx,
                "retweets": 5,
                "replies": 3,
                "score": 10,
                "thesis_quality": 7,
                "created_at": utc_now(),
            }
        )

    evidence = build_social_evidence(tweets, ticker="PAGE", address=token_ca, min_count=1, max_tweets=24)
    page_1 = format_research_social_block("PAGE", [], [], social_evidence=evidence, address=token_ca, page=1)
    page_2 = format_research_social_block("PAGE", [], [], social_evidence=evidence, address=token_ca, page=2)
    keyboard = build_xsignal_pagination_keyboard(token_ca, evidence, page=1)

    assert "Shown page" not in page_1
    assert "Shown page" not in page_2
    assert "qualified tweets selected" not in page_1
    assert page_1.count("· 👁") == 6
    assert page_2.count("· 👁") == 6
    assert "(Score:" not in page_1
    assert "Tier S" not in page_1
    assert keyboard is not None
    labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
    assert labels == ["← Prev", "Page 1/3", "Next →"]


def test_research_and_signal_cards_do_not_append_contract_command():
    from main import format_research_card, format_signal_telegram

    token_ca = ca(71)
    research = format_research_card(
        token_name="Clean Card",
        ticker="CLEAN",
        address=token_ca,
        dex={"mcap": 100_000, "volume_24h": 50_000, "liquidity": 40_000, "price_change_1h": 2.0},
        deployer_info=None,
        x_mentions=[],
        influencer_mentions=[],
        social_evidence={},
    )
    signal = format_signal_telegram(
        {"source": "coingecko", "name": "Clean Card", "symbol": "CLEAN", "address": token_ca},
        {"mcap": 100_000, "volume_24h": 50_000, "liquidity": 40_000, "price_change_1h": 2.0},
    )

    assert f"/research {token_ca}" not in research
    assert f"/research {token_ca}" not in signal
    assert not research.rstrip().endswith(token_ca)
    assert not signal.rstrip().endswith(token_ca)


@pytest.mark.asyncio
async def test_ticker_x_callback_searches_ticker_top_not_contract(monkeypatch):
    import main

    calls = []
    sent = []
    tasks = []

    async def fake_answer(*args, **kwargs):
        return True

    async def fake_send(session, text, chat_id="", reply_markup=None):
        sent.append(text)
        return 1

    async def fake_search_x_mentions(session, ticker, token_name="", address="", **kwargs):
        calls.append({"ticker": ticker, "address": address, **kwargs})
        return [
            {
                "username": "ticker_top",
                "url": "https://x.com/ticker_top/status/1",
                "text": "$GSPEED has a strong thesis and visible market attention.",
                "date": "2026-06-04",
                "likes": 7,
                "views": 766,
                "retweets": 1,
            }
        ]

    def fake_create_task(coro):
        tasks.append(coro)
        return coro

    monkeypatch.setattr(main, "answer_callback_query", fake_answer)
    monkeypatch.setattr(main, "send_telegram", fake_send)
    monkeypatch.setattr(main, "search_x_mentions", fake_search_x_mentions)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    await main.handle_trade_callback(
        None,
        {
            "id": "cb-1",
            "data": f"xtickerx:GSPEED:{ca(72)[:12]}",
            "message": {"chat": {"id": 544999608}, "message_id": 99},
            "from": {"username": "tester"},
        },
    )
    assert len(tasks) == 1
    await tasks[0]

    assert calls == [
        {
            "ticker": "GSPEED",
            "address": "",
            "max_age_hours": 24,
            "allow_tier3": True,
            "limit": 12,
            "min_count": 0,
        }
    ]
    assert sent and "Top tweets: $GSPEED" in sent[0]
    assert "ticker_top" in sent[0]


def test_hermes_context_loads_default_rule_files():
    from services.hermes_context import build_hermes_system_prompt, load_hermes_context

    context = load_hermes_context()
    prompt = build_hermes_system_prompt()

    assert context["loaded"] is True
    assert context["missing"] == []
    assert set(context["files"]) == {"identity.md", "agents.md", "memory.md"}
    assert "Hermes Agent Identity" in prompt
    assert "Hermes Agent Rules" in prompt
    assert "Hermes Agent Memory Policy" in prompt


def test_research_with_ca_requires_contract_mentions_not_ticker_only():
    from main import (
        build_research_query,
        build_x_research_url,
        filter_research_tweets,
        parse_socialdata_tweet,
        research_relevance,
    )

    token_ca = ca(60)
    ticker_only = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=60,
            text="$CUE thesis undervalued volume holders onchain catalyst",
            views=500,
            likes=20,
        )
    )
    ca_mention = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=61,
            text=f"{token_ca} $CUE thesis undervalued volume holders onchain catalyst",
            views=500,
            likes=20,
        )
    )

    assert build_research_query("CUE", token_ca) == f'"{token_ca}"'
    assert "OR%20$CUE" in build_x_research_url(token_ca, "CUE")
    assert research_relevance(ticker_only, "CUE", token_ca) is False
    assert research_relevance(ca_mention, "CUE", token_ca) is True
    assert filter_research_tweets([ticker_only], ticker="CUE", address=token_ca, allow_tier3=True) == []


def test_tweet_provenance_marks_ca_and_cashtag_sources():
    from main import filter_research_tweets, parse_socialdata_tweet

    token_ca = ca(73)
    ca_mention = parse_socialdata_tweet(
        socialdata_tweet(
            tweet_id=173,
            text=f"{token_ca} $SRC utility protocol thesis with volume and holder traction",
            views=900,
            likes=20,
        )
    )
    selected = filter_research_tweets(
        [ca_mention],
        ticker="SRC",
        address=token_ca,
        allow_tier3=True,
        min_count=0,
    )

    assert len(selected) == 1
    assert selected[0]["source_match"] == "ca"
    assert selected[0]["ca_confirmed"] is True
    assert selected[0]["ticker_confirmed"] is True
    assert selected[0]["source_provider"] == "socialdata"


def test_social_evidence_with_ca_drops_ticker_only_and_keeps_provenance():
    from services.social_evidence import build_social_evidence

    token_ca = ca(74)
    ticker_only = {
        "username": "ticker_only",
        "url": "https://x.com/ticker_only/status/1",
        "text": "$SAFE utility protocol thesis with real traction and volume context.",
        "views": 2_000,
        "likes": 30,
        "retweets": 4,
        "created_at": utc_now(),
    }
    ca_linked = {
        "username": "ca_linked",
        "url": "https://x.com/ca_linked/status/2",
        "text": f"{token_ca} $SAFE utility protocol thesis with real traction and volume context.",
        "views": 2_000,
        "likes": 30,
        "retweets": 4,
        "created_at": utc_now(),
    }

    evidence = build_social_evidence(
        [ticker_only, ca_linked],
        ticker="SAFE",
        address=token_ca,
        min_count=1,
    )

    assert [item["username"] for item in evidence["top_tweets"]] == ["ca_linked"]
    assert evidence["top_tweets"][0]["source_match"] == "ca"
    assert evidence["source_provenance"]["ca_confirmed"] == 1


def test_hybrid_social_evidence_keeps_ticker_context_without_verdict_credit():
    from services.social_evidence import build_social_evidence

    token_ca = ca(75)
    ticker_context = {
        "username": "ticker_context",
        "url": "https://x.com/ticker_context/status/1",
        "text": "$HYBRID privacy protocol thesis with real Base traction and strong narrative context.",
        "views": 8_000,
        "likes": 80,
        "retweets": 10,
        "followers": 12_000,
        "created_at": utc_now(),
    }
    ca_linked = {
        "username": "ca_linked",
        "url": "https://x.com/ca_linked/status/2",
        "text": f"{token_ca} $HYBRID privacy protocol thesis with real Base traction and strong narrative context.",
        "views": 500,
        "likes": 10,
        "retweets": 1,
        "created_at": utc_now(),
    }

    evidence = build_social_evidence(
        [ticker_context, ca_linked],
        ticker="HYBRID",
        address=token_ca,
        min_count=2,
        include_context=True,
    )

    assert len(evidence["top_tweets"]) == 2
    assert evidence["qualified"] is False
    assert evidence["qualified_tweets"] == 1
    assert evidence["trust_summary"]["ca_confirmed"] == 1
    assert evidence["trust_summary"]["ticker_strong"] == 1
    assert evidence["top_tweets"][0]["evidence_type"] == "ca_confirmed"
    assert evidence["top_tweets"][1]["evidence_type"] == "ticker_strong"


def test_x_signal_hybrid_summary_shows_ca_and_ticker_context():
    from main import format_research_social_block
    from services.social_evidence import build_social_evidence

    token_ca = ca(76)
    evidence = build_social_evidence(
        [
            {
                "username": "ticker_context",
                "url": "https://x.com/ticker_context/status/1",
                "text": "$SUM utility product narrative with real attention and market traction.",
                "views": 8_000,
                "likes": 80,
                "retweets": 10,
                "followers": 12_000,
                "created_at": utc_now(),
            }
        ],
        ticker="SUM",
        address=token_ca,
        min_count=1,
        include_context=True,
    )
    block = format_research_social_block("SUM", [], [], social_evidence=evidence, address=token_ca)

    assert "CA proof:" in block
    assert "none" in block
    assert "Ticker context:" in block
    assert "narrative exists, but attribution to this CA is not confirmed" in block


@pytest.mark.asyncio
async def test_ca_social_confirmation_blocks_ticker_only_social_proof(monkeypatch):
    import main

    token_ca = ca(61)

    calls = {"socialdata": 0}

    async def fake_search_x_mentions(session, ticker, token_name="", address="", **kwargs):
        calls["socialdata"] += 1
        return [{"url": "https://x.com/researcher/status/1", "text": "$CUE ticker-only thesis"}]

    async def fake_search_nitter_mentions(session, ticker, address="", limit=12, max_age_hours=24):
        return []

    monkeypatch.setattr(main, "SOCIALDATA_API_KEY", "test-key")
    monkeypatch.setattr(main, "REQUIRE_CA_SOCIAL_CONFIRMATION", True)
    monkeypatch.setattr(main, "search_x_mentions", fake_search_x_mentions)
    monkeypatch.setattr(main, "search_nitter_mentions", fake_search_nitter_mentions)

    passed, reason, evidence = await main.validate_ca_social_confirmation(
        None,
        ticker="CUE",
        address=token_ca,
    )

    assert passed is False
    assert "no Nitter alpha for CA" in reason
    assert evidence["verified"] is False
    assert calls["socialdata"] == 0


@pytest.mark.asyncio
async def test_manual_analysis_persists_ca_social_evidence(db_url, monkeypatch):
    import main

    token_ca = ca(62)

    async def fake_fetch_geckoterminal(session, address):
        return {
            "token_name": "Manual Good",
            "token_symbol": "MGD",
            "mcap": 155_000,
            "volume_24h": 125_000,
            "liquidity": 92_000,
        }

    async def fake_validate_ca_social_confirmation(session, *, ticker, address):
        return True, "6 CA-qualified X mention(s)", {
            "enabled": True,
            "verified": True,
            "query_mode": "ca_only",
            "qualified_tweets": 6,
            "min_required": 5,
            "total_followers": 76_000,
            "total_likes": 180,
            "total_retweets": 24,
            "max_score": 14,
            "avg_thesis_quality": 5.5,
            "top_authors": [{"username": "analyst", "followers": 55_000, "score": 14, "tier": 2}],
        }

    monkeypatch.setattr(main, "fetch_geckoterminal", fake_fetch_geckoterminal)
    monkeypatch.setattr(main, "validate_ca_social_confirmation", fake_validate_ca_social_confirmation)

    launch, dex = await main.ensure_launch_for_analysis(None, token_ca)

    assert dex["mcap"] == 155_000
    assert launch["social_confirmation"]["verified"] is True
    async with db_session() as db:
        row = await main.get_launch(db, token_ca)
        assert row.raw_json["social_confirmation"]["qualified_tweets"] == 6


class FakeSocialDataResponse:
    status = 200

    def __init__(self, payload: dict):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return ""


class FakeSocialDataSession:
    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.params: list[dict] = []

    def get(self, url, *, headers, params, timeout):
        self.params.append(params)
        return FakeSocialDataResponse(self.pages[len(self.params) - 1])


@pytest.mark.asyncio
async def test_socialdata_search_uses_cursor_pagination_until_qualified_limit(db_url, monkeypatch):
    import main

    monkeypatch.setattr(main, "SOCIALDATA_API_KEY", "test-key")
    main.socialdata_search_cache.clear()
    main.socialdata_search_inflight.clear()
    first_page = {
        "tweets": [
            socialdata_tweet(tweet_id=30, text="Check https://OKAI.HK/ALpha $CUE", views=1_000, likes=100),
            socialdata_tweet(tweet_id=31, text="$CUE strong thesis volume holders", views=40, likes=20),
        ],
        "next_cursor": "cursor-2",
    }
    second_page = {
        "tweets": [
            socialdata_tweet(
                tweet_id=32 + i,
                username=f"pager{i}",
                text=f"$CUE unique research thesis volume holders catalyst {i}",
                views=500,
                likes=20,
            )
            for i in range(5)
        ],
    }
    session = FakeSocialDataSession([first_page, second_page])

    results = await main.socialdata_search(session, "$CUE", limit=5, max_pages=2)

    assert len(results) == 5
    assert session.params[0] == {"query": "$CUE", "type": "Top"}
    assert session.params[1]["cursor"] == "cursor-2"

    cached_session = FakeSocialDataSession([])
    cached = await main.socialdata_search(cached_session, "$CUE", limit=5, max_pages=2)
    assert len(cached) == 5
    assert cached_session.params == []


@pytest.mark.asyncio
async def test_provider_cooldown_datetime_survives_sqlite_roundtrip(db_url):
    async with db_session() as db:
        await set_provider_cooldown(
            db,
            provider="geckoterminal",
            cooldown_until=utc_now() + timedelta(minutes=1),
            reason="429",
        )

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        assert await provider_available(db, "geckoterminal") is False


@pytest.mark.asyncio
async def test_spoof_detector_flags_same_ticker_collisions_that_pass_filters(db_url, monkeypatch):
    monkeypatch.setattr(spoof_detector, "SAME_TICKER_EXTERNAL_ENABLED", False)
    now_ms = int(utc_now().timestamp() * 1000)
    current_ca = ca(20)
    matching_ca = ca(21)
    weak_ca = ca(22)
    old_ca = ca(23)

    async with db_session() as db:
        for token_ca, launched_at in (
            (current_ca, utc_now()),
            (matching_ca, utc_now() - timedelta(minutes=15)),
            (weak_ca, utc_now() - timedelta(minutes=20)),
            (old_ca, utc_now() - timedelta(hours=5)),
        ):
            await upsert_launch(
                db,
                ca=token_ca,
                ticker="DUPE",
                name="Dupe",
                source="dexscreener",
                raw_json={"address": token_ca, "symbol": "DUPE", "source": "dexscreener"},
                launched_at=launched_at,
                status="new",
            )

        await mark_launch_status(
            db,
            ca=matching_ca,
            status="signaled",
            market_json={
                "mcap": 75_000,
                "volume_24h": 45_000,
                "liquidity": 42_000,
                "pair_created_at": now_ms - 15 * 60 * 1000,
            },
        )
        await mark_launch_status(
            db,
            ca=weak_ca,
            status="queued_recheck",
            market_json={
                "mcap": 20_000,
                "volume_24h": 10_000,
                "liquidity": 40_000,
                "pair_created_at": now_ms - 20 * 60 * 1000,
            },
        )
        await mark_launch_status(
            db,
            ca=old_ca,
            status="signaled",
            market_json={
                "mcap": 90_000,
                "volume_24h": 55_000,
                "liquidity": 50_000,
                "pair_created_at": now_ms - 5 * 60 * 60 * 1000,
            },
        )

        signals = await detect_spoof_signals(
            db,
            ca=current_ca,
            ticker="DUPE",
            dex={
                "mcap": 80_000,
                "volume_24h": 50_000,
                "liquidity": 50_000,
                "pair_created_at": now_ms,
            },
            research_data={"source": {"source": "dexscreener"}, "market": {"age_minutes": 1}},
        )

    ticker_signals = [signal for signal in signals if signal["type"] == "same_ticker_fresh_passed_filters"]
    assert len(ticker_signals) == 1
    assert "1 other fresh $DUPE pair passed filters" in ticker_signals[0]["title"]
    matches = ticker_signals[0]["evidence"]["matches"]
    assert [match["ca"] for match in matches] == [matching_ca]

    prior_signals = [signal for signal in signals if signal["type"] == "same_ticker_prior_passed_filters"]
    assert len(prior_signals) == 1
    assert "prior $DUPE market passed filters" in prior_signals[0]["title"]
    prior_matches = prior_signals[0]["evidence"]["matches"]
    assert [match["ca"] for match in prior_matches] == [old_ca]


@pytest.mark.asyncio
async def test_verdict_v21_rewards_ca_verified_social_proof(db_url, monkeypatch):
    monkeypatch.setattr(spoof_detector, "SAME_TICKER_EXTERNAL_ENABLED", False)
    token_ca = ca(42)
    market = {
        "mcap": 155_000,
        "volume_24h": 125_000,
        "liquidity": 92_000,
        "price_usd": "0.001",
        "price_change_1h": 26,
        "pair_created_at": int(utc_now().timestamp() * 1000) - 18 * 60 * 1000,
        "txns_h1_buys": 31,
        "txns_h1_sells": 16,
    }
    raw = {
        "address": token_ca,
        "symbol": "GOOD",
        "name": "Good Token",
        "source": "coingecko",
        "description": "A Base-native social trading research tool with early community traction.",
        "social_confirmation": {
            "verified": True,
            "qualified_tweets": 6,
            "min_required": 5,
            "total_followers": 76_000,
            "total_likes": 180,
            "total_retweets": 24,
            "max_score": 14,
            "avg_thesis_quality": 5.5,
            "social_evidence": {
                "thesis": "Social evidence frames this as utility/tech rather than pure meme; 6 qualified CA-linked tweets.",
                "project_value": "Utility / Tech",
                "project_value_score": 18,
                "evidence_count": 6,
                "top_tweets": [
                    {
                        "ref": 1,
                        "username": "analyst",
                        "url": "https://x.com/analyst/status/1",
                        "views": 12_000,
                        "likes": 180,
                        "score": 72,
                        "reason": "utility/tech context, engagement",
                    }
                ],
            },
            "top_authors": [{"username": "analyst", "followers": 55_000, "score": 14, "tier": 2}],
        },
    }

    async with db_session() as db:
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="GOOD",
            name="Good Token",
            source="coingecko",
            raw_json=raw,
            launched_at=utc_now(),
            status="new",
        )
        await mark_launch_status(db, ca=token_ca, status="signaled", market_json=market, raw_json=raw)
        research = await run_research_pipeline(db, ca=token_ca, dex=market, requested_by="test_verdict")
        await detect_spoof_signals(
            db,
            ca=token_ca,
            ticker="GOOD",
            dex=market,
            research_data=research["processed_data"],
        )
        verdict = await build_verdict_v2(db, ca=token_ca, launch=raw, dex=market)

    assert verdict["version"] == "verdict-v2.1"
    assert verdict["label"] == "WATCH"
    assert verdict["score"] >= 72
    assert verdict["categories"]["social"] >= 20
    assert verdict["research"]["social"]["ca_verified"] is True
    assert "Evidence:" in verdict["human_readable"]
    assert "[1] @analyst" in verdict["human_readable"]


def test_geckoterminal_same_ticker_parser_keeps_only_exact_base_symbol_candidates():
    current_ca = ca(40)
    older_ca = ca(41)
    now = utc_now() - timedelta(hours=17)
    data = {
        "data": [
            {
                "attributes": {
                    "address": ca(900),
                    "name": "CUE / WETH",
                    "pool_created_at": now.isoformat().replace("+00:00", "Z"),
                    "fdv_usd": "105000",
                    "volume_usd": {"h24": "136000"},
                    "reserve_in_usd": "72000",
                    "price_change_percentage": {"h1": "320"},
                    "transactions": {"h1": {"buys": 10, "sells": 8}, "h24": {"buys": 20, "sells": 12}},
                },
                "relationships": {
                    "base_token": {"data": {"id": f"base_{older_ca}"}},
                    "dex": {"data": {"id": "uniswap-v4-base"}},
                },
            },
            {
                "attributes": {
                    "address": ca(901),
                    "name": "BASEMATE / WETH",
                    "pool_created_at": now.isoformat().replace("+00:00", "Z"),
                    "fdv_usd": "999999",
                    "volume_usd": {"h24": "999999"},
                    "reserve_in_usd": "999999",
                },
                "relationships": {
                    "base_token": {"data": {"id": f"base_{ca(42)}"}},
                    "dex": {"data": {"id": "uniswap-v4-base"}},
                },
            },
            {
                "attributes": {
                    "address": ca(902),
                    "name": "CUE / WETH",
                    "pool_created_at": now.isoformat().replace("+00:00", "Z"),
                    "fdv_usd": "150000",
                    "volume_usd": {"h24": "150000"},
                    "reserve_in_usd": "150000",
                },
                "relationships": {
                    "base_token": {"data": {"id": f"base_{current_ca}"}},
                    "dex": {"data": {"id": "uniswap-v4-base"}},
                },
            },
        ]
    }

    candidates = spoof_detector.parse_geckoterminal_same_ticker_candidates(
        data,
        ticker="CUE",
        current_ca=current_ca,
    )
    assert [item["ca"] for item in candidates] == [older_ca]
    assert spoof_detector.passes_same_ticker_candidate_filters(
        candidates[0],
        min_age_seconds=spoof_detector.MAX_TOKEN_AGE,
        max_age_seconds=spoof_detector.SAME_TICKER_PRIOR_LOOKBACK.total_seconds(),
    )


def test_project_narrative_uses_qualified_x_evidence_without_screener_description():
    token_ca = ca(50)
    tweets = [
        {
            "username": f"analyst{i}",
            "text": f"{token_ca} $VEIL is building private shielded swaps for Base users and MEV-resistant trading.",
            "views": 2000,
            "likes": 25,
        }
        for i in range(3)
    ]
    narrative = extract_project_narrative(
        ca=token_ca,
        ticker="VEIL",
        name="Veil",
        social_evidence={"qualified_tweets": 3, "top_tweets": tweets},
    )

    assert narrative.confidence == "MEDIUM"
    assert "privacy" in narrative.product.lower() or "shielded" in narrative.product.lower()
    assert narrative.ca_confirmed_mentions == 3
    assert narrative.is_ticker_only_evidence is False


def test_project_narrative_does_not_infer_product_from_ticker_only():
    narrative = extract_project_narrative(
        ca=ca(51),
        ticker="VEIL",
        name="Veil",
        social_evidence={"qualified_tweets": 0, "top_tweets": []},
    )

    assert narrative.confidence == "LOW"
    assert "No verified project description found" not in narrative.product
    assert "weak signals" in narrative.product


def test_project_narrative_infers_surplus_ai_inference_from_metadata():
    narrative = extract_project_narrative(
        ca=ca(77),
        ticker="SURPLUS",
        name="Surplus Intelligence",
        dex={
            "token_name": "Surplus Intelligence",
            "token_symbol": "SURPLUS",
            "websites": [{"url": "https://surplusintelligence.ai"}],
            "socials": [{"type": "twitter", "url": "https://x.com/surplusintelligence"}],
        },
        social_evidence={
            "qualified_tweets": 0,
            "top_tweets": [
                {
                    "username": "researcher",
                    "text": "$SURPLUS is an AI inference marketplace for decentralized intelligence on Base.",
                    "views": 8_000,
                    "likes": 80,
                    "evidence_type": "ticker_strong",
                }
            ],
        },
    )

    assert "No verified project description found" not in narrative.product
    assert "AI inference" in narrative.product
    assert "intelligence" in narrative.product.lower()
