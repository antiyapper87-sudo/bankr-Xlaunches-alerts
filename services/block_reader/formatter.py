from __future__ import annotations

import html

from services.block_reader.types import BlockRiskSummary


def h(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def risk_label(score: float, confidence: str = "MEDIUM") -> str:
    if str(confidence or "").upper() == "LOW":
        return "UNKNOWN"
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def launch_quality(summary: BlockRiskSummary) -> str:
    if summary.confidence == "LOW":
        return "WAIT"
    if summary.overall_risk_score >= 50:
        return "SKIP"
    if summary.overall_risk_score >= 25:
        return "HIGH RISK"
    return "WATCH"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def format_onchain_block(summary: BlockRiskSummary | dict | None) -> str:
    if isinstance(summary, dict):
        summary = BlockRiskSummary(
            bundle_risk=float(summary.get("bundle_risk") or summary.get("bundle_risk_score") or 0),
            sniper_score=float(summary.get("sniper_score") or 0),
            prebuy_risk=float(summary.get("prebuy_risk") or 0),
            dev_dump_risk=float(summary.get("dev_dump_risk") or summary.get("dev_risk_score") or 0),
            liquidity_risk=float(summary.get("liquidity_risk") or summary.get("liquidity_risk_score") or 0),
            holder_concentration_risk=float(summary.get("holder_concentration_risk") or summary.get("holder_concentration_score") or 0),
            confidence=str(summary.get("confidence") or "LOW").upper(),
            suspected_bundle_wallets_count=int(summary.get("suspected_bundle_wallets_count") or 0),
            bundle_total_bought_pct=summary.get("bundle_total_bought_pct"),
            bundle_current_held_pct=summary.get("bundle_current_held_pct"),
            bundle_sold_pct=summary.get("bundle_sold_pct"),
            first_buyers_count=int(summary.get("first_buyers_count") or 0),
        )

    if not summary:
        return (
            "🧱 <b>On-chain</b>\n\n"
            "Bundle risk: <b>UNKNOWN</b>\n"
            "• On-chain scan not available yet\n"
            "• Deep scan queued if token remains interesting"
        )

    bundle = risk_label(summary.bundle_risk, summary.confidence)
    dev = risk_label(summary.dev_dump_risk, summary.confidence)
    quality = launch_quality(summary)
    lines = ["🧱 <b>On-chain</b>", "", f"Bundle risk: <b>{bundle}</b>"]

    if summary.suspected_bundle_wallets_count:
        lines.extend([
            f"• {summary.suspected_bundle_wallets_count} linked early wallets bought {_fmt_pct(summary.bundle_total_bought_pct)} supply",
            f"• Still holding: {_fmt_pct(summary.bundle_current_held_pct)}",
            f"• Sold: {_fmt_pct(summary.bundle_sold_pct)} of bundle allocation",
        ])
    else:
        lines.append("• No same-block early-wallet cluster detected")

    lines.extend(["", f"Dev risk: <b>{dev}</b>"])
    if summary.dev_dump_risk >= 25:
        lines.append("• Deployer-linked behavior detected; verify before acting")
    else:
        lines.append("• No deployer sell/funding link detected in quick window")

    lines.extend(["", f"Launch quality: <b>{quality}</b>"])
    lines.append(f"• First {summary.first_buyers_count} buyers checked")
    if summary.liquidity_risk >= 25:
        lines.append("• Liquidity removal / liquidity anomaly detected")
    elif summary.sniper_score >= 25:
        lines.append("• Sniper-like first-block entry pattern detected")
    else:
        lines.append("• No quick liquidity removal detected")
    if summary.confidence == "LOW":
        lines.append("• Confidence LOW: deep scan needed")
    return "\n".join(lines)[:1200]
