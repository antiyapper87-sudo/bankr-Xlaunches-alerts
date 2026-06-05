from __future__ import annotations

from services.block_reader.bundle_detector import detect_bundle_clusters
from services.block_reader.constants import TRANSFER_TOPIC
from services.block_reader.formatter import format_onchain_block
from services.block_reader.sniper_detector import detect_sniper_patterns
from services.block_reader.token_events import compute_positions_from_transfers, decode_transfer_log, first_buyers_from_transfers
from services.block_reader.types import BlockRiskSummary, TokenTransfer
from services.verdict_v2 import label_for_score, score_onchain


def ca(n: int) -> str:
    return "0x" + f"{n:040x}"


def topic_addr(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def test_decode_erc20_transfer_log():
    log = {
        "address": ca(1),
        "transactionHash": "0x" + "a" * 64,
        "blockNumber": hex(123),
        "logIndex": hex(7),
        "topics": [TRANSFER_TOPIC, topic_addr(ca(2)), topic_addr(ca(3))],
        "data": hex(1000),
    }

    tx = decode_transfer_log(log)

    assert tx is not None
    assert tx.from_address == ca(2)
    assert tx.to_address == ca(3)
    assert tx.amount_raw == 1000
    assert tx.block_number == 123


def test_first_buyers_and_positions_from_pair_transfers():
    pair = ca(9)
    transfers = [
        TokenTransfer("0x1", 100, 1, ca(1), pair, ca(101), 100),
        TokenTransfer("0x2", 101, 2, ca(1), pair, ca(102), 200),
        TokenTransfer("0x3", 102, 3, ca(1), ca(101), pair, 40),
        TokenTransfer("0x4", 103, 4, ca(1), ca(500), ca(501), 999),
    ]

    buyers = first_buyers_from_transfers(transfers, pair_address=pair, limit=10)
    positions = compute_positions_from_transfers(transfers, pair_address=pair, wallets={item.wallet for item in buyers})

    assert [item.wallet for item in buyers] == [ca(101), ca(102)]
    assert positions[ca(101)].bought_raw == 100
    assert positions[ca(101)].sold_raw == 40
    assert positions[ca(102)].bought_raw == 200


def test_bundle_cluster_scores_held_and_sold_allocation():
    positions = []
    for idx in range(6):
        positions.append(
            type(
                "P",
                (),
                {
                    "wallet": ca(200 + idx),
                    "first_buy_block": 100,
                    "first_buy_tx": f"0x{idx}",
                    "bought_raw": 100,
                    "sold_raw": 60 if idx < 3 else 0,
                    "current_balance_raw": 40 if idx < 3 else 100,
                },
            )()
        )

    result = detect_bundle_clusters(positions, total_supply_raw=2_000, pair_created_block=100)

    assert result["risk_score"] >= 35
    assert result["suspected_wallets_count"] == 6
    assert result["total_bought_pct"] == 30.0
    assert result["current_held_pct"] == 21.0
    assert result["sold_pct_of_allocation"] == 30.0


def test_onchain_formatter_low_confidence_is_unknown():
    text = format_onchain_block(BlockRiskSummary(confidence="LOW", bundle_risk=90, first_buyers_count=0))

    assert "Bundle risk: <b>UNKNOWN</b>" in text
    assert "Confidence LOW" in text


def test_sniper_detector_flags_first_block_cluster():
    positions = []
    for idx in range(10):
        positions.append(
            type(
                "P",
                (),
                {
                    "wallet": ca(300 + idx),
                    "first_buy_block": 100 + (idx % 2),
                    "bought_raw": 100,
                },
            )()
        )

    result = detect_sniper_patterns(positions, total_supply_raw=3_000, pair_created_block=100)

    assert result["risk_score"] >= 35
    assert result["first_5_block_wallets"] == 10


def test_verdict2_onchain_high_risk_blocks_watch():
    score, reasons, risks = score_onchain({
        "onchain": {
            "provider": "alchemy",
            "confidence": "HIGH",
            "bundle_risk_score": 82,
            "suspected_bundle_wallets_count": 11,
        }
    })

    assert score <= 1
    assert any("on-chain" in risk for risk in risks)
    assert label_for_score(70, risks) == "HIGH RISK"
