from __future__ import annotations

import base64
import html
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp


def _load_local_env(path: str = ".env.local") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()

FOMO_ENABLED = os.getenv("FOMO_ENABLED", "false").lower() == "true"
FOMO_API_BASE = os.getenv("FOMO_API_BASE", "https://prod-api.fomo.family").rstrip("/")
FOMO_COOKIES_FILE = Path(os.getenv("FOMO_COOKIES_FILE", ".secrets/fomo_cookies.json"))
FOMO_SUPPORTED_CHAINS = os.getenv("FOMO_SUPPORTED_CHAINS", "56,143,8453,1399811149")
FOMO_DEFAULT_CHAIN_ID = int(os.getenv("FOMO_DEFAULT_CHAIN_ID", "8453"))
FOMO_WEB_BASE = "https://fomo.family"


class FomoAuthError(RuntimeError):
    pass


def _b64json(part: str) -> dict[str, Any]:
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())


def _load_cookie_export() -> list[dict[str, Any]]:
    if not FOMO_COOKIES_FILE.exists():
        raise FomoAuthError(f"Fomo cookies file missing: {FOMO_COOKIES_FILE}")
    try:
        raw = json.loads(FOMO_COOKIES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FomoAuthError("Fomo cookies file is not valid JSON") from exc
    if not isinstance(raw, list):
        raise FomoAuthError("Fomo cookies export must be a JSON array")
    return [item for item in raw if isinstance(item, dict)]


def load_fomo_token() -> str:
    cookies = _load_cookie_export()
    token = next((str(c.get("value") or "") for c in cookies if c.get("name") == "privy-token"), "")
    if not token:
        raise FomoAuthError("privy-token not found in Fomo cookies")

    try:
        payload = _b64json(token.split(".")[1])
        exp = int(payload.get("exp") or 0)
    except Exception as exc:
        raise FomoAuthError("invalid privy-token JWT") from exc

    if exp <= int(time.time()) + 60:
        raise FomoAuthError("Fomo privy-token expired or expires in <60s")
    return token


def load_fomo_cookie_header() -> str:
    cookies = _load_cookie_export()
    parts: list[str] = []
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def fomo_headers() -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": FOMO_WEB_BASE,
        "referer": f"{FOMO_WEB_BASE}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "authorization": f"Bearer {load_fomo_token()}",
        "cookie": load_fomo_cookie_header(),
        "x-supported-chains": FOMO_SUPPORTED_CHAINS,
    }


def build_fomo_url(address: str, chain_id: int = FOMO_DEFAULT_CHAIN_ID) -> str:
    return f"{FOMO_WEB_BASE}/coin?address={address}&chainId={int(chain_id)}"


async def fetch_fomo_top_holders(
    session: aiohttp.ClientSession,
    *,
    address: str,
    chain_id: int = FOMO_DEFAULT_CHAIN_ID,
) -> dict[str, Any]:
    token_payload = json.dumps(
        [{"address": address, "networkId": int(chain_id)}],
        separators=(",", ":"),
    )
    url = f"{FOMO_API_BASE}/hodlers/top?tokens={quote(token_payload)}"

    async with session.get(url, headers=fomo_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
        try:
            data = await resp.json(content_type=None)
        except Exception as exc:
            content_type = resp.headers.get("content-type", "")
            raise FomoAuthError(f"Fomo holders returned non-JSON response: {resp.status} {content_type}") from exc
        if resp.status != 200 or not data.get("success"):
            message = str(data.get("message") or "request failed")
            raise FomoAuthError(f"Fomo holders failed: {resp.status} {message[:160]}")
        items = data.get("responseObject") or []
        return items[0] if items else {"topHolders": [], "totalHolders": 0}


async def fetch_fomo_feed(
    session: aiohttp.ClientSession,
    *,
    address: str,
    chain_id: int = FOMO_DEFAULT_CHAIN_ID,
    thesis_only: bool = False,
) -> dict[str, Any]:
    endpoint = "/feed/token/thesis" if thesis_only else "/feed/token"
    url = (
        f"{FOMO_API_BASE}{endpoint}"
        f"?tokenAddress={address}&networkId={int(chain_id)}"
        f"{'' if thesis_only else '&excludeThesis=true'}"
    )
    async with session.get(url, headers=fomo_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
        try:
            data = await resp.json(content_type=None)
        except Exception as exc:
            content_type = resp.headers.get("content-type", "")
            raise FomoAuthError(f"Fomo feed returned non-JSON response: {resp.status} {content_type}") from exc
        if resp.status != 200 or not data.get("success"):
            message = str(data.get("message") or "request failed")
            raise FomoAuthError(f"Fomo feed failed: {resp.status} {message[:160]}")
        return data.get("responseObject") or {}


def _money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if abs(amount) >= 1_000:
        return f"${amount / 1_000:.1f}K"
    return f"${amount:,.0f}"


def _price(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"${amount:.8f}" if amount else "n/a"


def format_fomo_holders_card(result: dict[str, Any], symbol: str = "") -> str:
    holders = result.get("topHolders") or []
    total = int(result.get("totalHolders") or 0)
    suffix = f" ${html.escape(symbol.lstrip('$'))}" if symbol else ""
    lines = [f"👥 <b>Fomo holders</b>{suffix}"]
    if total:
        lines.append(f"Total: <b>{total:,}</b>")
    if not holders:
        return "\n".join(lines + ["No holders found."])

    for idx, holder in enumerate(holders[:10], 1):
        user = holder.get("user") or {}
        handle = html.escape(str(user.get("userHandle") or user.get("displayName") or "unknown"))
        value = float(holder.get("value") or 0)
        pnl = float(holder.get("pnl") or 0)
        cost_basis = float(holder.get("costBasis") or 0)
        pnl_pct = (pnl / cost_basis * 100) if cost_basis else None
        avg = holder.get("averageEntryPrice")
        lines.append(
            f"\n{idx}. <b>@{handle}</b>"
            f"\n   Pos: {_money(value)} · PnL: {pnl:+,.0f}$"
            + (f" ({pnl_pct:+.1f}%)" if pnl_pct is not None else "")
            + (f"\n   Avg entry: {_price(avg)}" if avg else "")
        )
    return "\n".join(lines)[:3900]
