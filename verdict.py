"""
verdict.py — Auto-research & legitimacy scoring for Whale Alert Bot
===================================================================
Runs AFTER a signal is sent (as a background task) and edits the
Telegram message in-place to append a 🧠 VERDICT block — so signal
latency is UNCHANGED.

HYBRID design:
  • SocialData gathers the HARD FACTS from X (exact follower counts,
    deployer bio, project-page existence, notable mentions).
  • Grok then INVESTIGATES X LIVE (X Search enabled) — reading the
    deployer account, the ticker, and any project account — and returns
    the final verdict, using both its own X findings and the SocialData
    facts (it's told the exact follower numbers are authoritative).

Falls back to a pure heuristic score if Grok is disabled / keyless / errors.

Self-contained: reads its own env vars, no circular imports with main.py.

Env vars:
  SOCIALDATA_API_KEY      — required (already set for the bot)
  XAI_API_KEY             — enables the Grok investigation pass
  GROK_MODEL              — default "grok-4.3"
  USE_GROK_VERDICT        — "true"/"false", default "true"
  GROK_LIVE_SEARCH        — "true"/"false", default "true" (lets Grok search X)
  GROK_MAX_SEARCH_RESULTS — default 12 (caps X Search cost per verdict)
  VERDICT_MIN_FOLLOWERS   — notable-mention follower floor, default 10000
"""

import os
import re
import json
import logging
import aiohttp

log = logging.getLogger("whale-alert")

# ─── Config ───────────────────────────────────────────────────────────────────

SOCIALDATA_API_KEY = os.getenv("SOCIALDATA_API_KEY", "")
SOCIALDATA_USER_URL = "https://api.socialdata.tools/twitter/user"
SOCIALDATA_SEARCH_URL = "https://api.socialdata.tools/twitter/search"

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.3")
USE_GROK_VERDICT = os.getenv("USE_GROK_VERDICT", "true").lower() == "true"
GROK_LIVE_SEARCH = os.getenv("GROK_LIVE_SEARCH", "true").lower() == "true"
GROK_MAX_SEARCH_RESULTS = int(os.getenv("GROK_MAX_SEARCH_RESULTS", "12"))

MENTION_FOLLOWER_FLOOR = int(os.getenv("VERDICT_MIN_FOLLOWERS", "10000"))

# Bio signals that suggest a real builder / project affiliation
ROLE_KEYWORDS = [
    "founder", "co-founder", "cofounder", "ceo", "cto", "core", "dev",
    "developer", "engineer", "building", "builder", "ex-", "former",
    "team", "contributor", "researcher", "protocol", "labs", "studio",
]

_profile_cache: dict[str, dict | None] = {}


# ─── SocialData: profile (followers + bio in ONE call) ────────────────────────

async def get_x_profile(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Fetch an X profile: followers + bio + name + verified. One SocialData call."""
    username = (username or "").lstrip("@").strip().lower()
    if not username:
        return None
    if username in _profile_cache:
        return _profile_cache[username]
    if not SOCIALDATA_API_KEY:
        return None

    profile = None
    try:
        url = f"{SOCIALDATA_USER_URL}/{username}"
        headers = {"Authorization": f"Bearer {SOCIALDATA_API_KEY}", "Accept": "application/json"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                followers = (data.get("public_metrics", {}) or {}).get("followers_count")
                if followers is None:
                    followers = data.get("followers_count")
                profile = {
                    "handle": data.get("screen_name", username),
                    "name": data.get("name", ""),
                    "followers": int(followers) if followers is not None else 0,
                    "bio": data.get("description", "") or "",
                    "verified": bool(data.get("verified") or data.get("is_blue_verified")),
                }
            elif resp.status == 404:
                profile = None
            elif resp.status == 429:
                log.warning(f"  ⚠️ SocialData rate limited on profile @{username}")
                return None  # transient — don't cache
            else:
                log.debug(f"get_x_profile @{username} -> {resp.status}")
    except Exception as e:
        log.debug(f"get_x_profile error @{username}: {e}")
        return None

    _profile_cache[username] = profile
    return profile


# ─── SocialData: project-page detection ───────────────────────────────────────

async def find_project_page(session: aiohttp.ClientSession, ticker: str, token_name: str = "") -> dict | None:
    """Probe for a dedicated project X account named after the ticker / name."""
    candidates: list[str] = []
    t = (ticker or "").lstrip("$").strip()
    if t:
        candidates.append(t)
    if token_name:
        nm = re.sub(r"[^A-Za-z0-9]", "", token_name)
        if nm and nm.lower() != t.lower():
            candidates.append(nm)

    for cand in candidates[:3]:
        prof = await get_x_profile(session, cand)
        if prof and prof["followers"] >= 500:
            prof["matched_as"] = cand
            return prof
    return None


# ─── SocialData: notable mentions ─────────────────────────────────────────────

async def get_notable_mentions(session: aiohttp.ClientSession, ticker: str, limit: int = 5) -> list[dict]:
    """Top X mentions of $TICKER from accounts above the follower floor."""
    if not SOCIALDATA_API_KEY:
        return []
    mentions = []
    try:
        headers = {"Authorization": f"Bearer {SOCIALDATA_API_KEY}", "Accept": "application/json"}
        params = {"query": f"${ticker.lstrip('$')} min_faves:5", "type": "Top"}
        async with session.get(
            SOCIALDATA_SEARCH_URL, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
        for tw in data.get("tweets", []):
            u = tw.get("user", {}) or {}
            f = u.get("followers_count", 0) or 0
            if f < MENTION_FOLLOWER_FLOOR:
                continue
            mentions.append({
                "username": u.get("screen_name", ""),
                "followers": f,
                "text": (tw.get("full_text") or tw.get("text") or "")[:200],
            })
        mentions.sort(key=lambda m: m["followers"], reverse=True)
    except Exception as e:
        log.debug(f"get_notable_mentions error ${ticker}: {e}")
    return mentions[:limit]


# ─── Heuristic scoring (baseline / fallback) ──────────────────────────────────

def _bio_project_signal(bio: str) -> bool:
    if not bio:
        return False
    low = bio.lower()
    has_at = "@" in bio
    has_link = bool(re.search(r"https?://|\.xyz|\.io|\.com|\.fi|\.app|\.eth", low))
    kw = any(k in low for k in ROLE_KEYWORDS)
    return has_at or has_link or kw


def _heuristic_score(deployer: dict | None, project: dict | None, mentions: list) -> tuple:
    score = 0.0
    reasons = []

    f = deployer.get("followers", 0) if deployer else 0
    if f >= 100_000:
        score += 4; reasons.append(f"deployer {f // 1000}K followers")
    elif f >= 50_000:
        score += 3.5; reasons.append(f"deployer {f // 1000}K followers")
    elif f >= 10_000:
        score += 3; reasons.append(f"deployer {f // 1000}K followers")
    elif f >= 5_000:
        score += 2; reasons.append(f"deployer {f // 1000}K followers")
    elif f >= 2_000:
        score += 1; reasons.append(f"deployer {f} followers")
    elif f >= 500:
        score += 0.5

    if deployer:
        if _bio_project_signal(deployer.get("bio", "")):
            score += 1.5
            reasons.append("deployer bio shows builder/project")
        if deployer.get("verified"):
            score += 0.5

    if project:
        pf = project.get("followers", 0)
        if pf >= 10_000:
            score += 3; reasons.append(f"project page {pf // 1000}K followers")
        elif pf >= 2_000:
            score += 2; reasons.append(f"project page {pf // 1000}K followers")
        elif pf >= 500:
            score += 1; reasons.append("project page exists")

    nm = len(mentions)
    if nm:
        score += min(nm * 0.6, 2.0)
        reasons.append(f"{nm} notable mention{'s' if nm != 1 else ''}")

    score = round(min(score, 10.0), 1)
    if score >= 7:
        label, emoji = "SOLID", "🟢"
    elif score >= 4:
        label, emoji = "MID", "🟡"
    else:
        label, emoji = "LIKELY SPAM", "🔴"
    return score, label, emoji, reasons


# ─── Grok (xAI) — LIVE X investigation + verdict ──────────────────────────────

async def _grok_judge(session: aiohttp.ClientSession, ticker: str, name: str, address: str,
                      deployer_handle: str, deployer: dict | None,
                      project: dict | None, mentions: list) -> dict | None:
    """Grok investigates X live (X Search) + judges. Returns None if disabled/fails."""
    if not (USE_GROK_VERDICT and XAI_API_KEY):
        return None
    try:
        known = {
            "ticker": ticker,
            "token_name": name,
            "contract": address,
            "deployer_handle": deployer_handle or (deployer or {}).get("handle", ""),
            "deployer_data_from_tools": deployer or {},
            "project_page_found_by_tools": project or {},
            "notable_mentions_from_tools": [
                {"handle": m["username"], "followers": m["followers"]} for m in mentions
            ],
        }
        system = (
            "You are a crypto launch analyst with LIVE access to X (Twitter). A new token just "
            "launched on Base. Investigate it on X and decide whether it's a low-effort spam launch "
            "or a credible one.\n\n"
            "Search X for:\n"
            "1) The DEPLOYER account - are they a real builder, do they have a following, do they "
            "work on a known project (check their bio/header)?\n"
            "2) The TICKER $<ticker> - who is talking about it, and are they credible accounts?\n"
            "3) Any official PROJECT account for this token.\n\n"
            "You are also given structured data our own tools already pulled (exact follower counts, "
            "bio, mentions). Treat those exact follower numbers as AUTHORITATIVE - prefer them over "
            "your own estimates - but use your live X search to judge legitimacy, find the project "
            "account, and read sentiment.\n\n"
            "Return ONLY compact JSON: {\"score\": <0-10 number>, \"label\": "
            "\"SOLID|MID|LIKELY SPAM\", \"reason\": \"one or two short sentences citing what you "
            "actually found on X\"}. No markdown, no text outside the JSON."
        )
        body = {
            "model": GROK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(known)},
            ],
            "temperature": 0.1,
            "max_tokens": 500,
        }
        if GROK_LIVE_SEARCH:
            body["search_parameters"] = {
                "mode": "on",
                "sources": [{"type": "x"}],
                "max_search_results": GROK_MAX_SEARCH_RESULTS,
                "return_citations": False,
            }

        headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
        async with session.post(
            XAI_API_URL, json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),  # live search + reasoning can be slow
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.warning(f"  ⚠️ Grok verdict {resp.status} for ${ticker}: {err[:160]}")
                return None
            data = await resp.json()

        content = data["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        return {
            "score": round(float(parsed.get("score", 0)), 1),
            "label": str(parsed.get("label", "MID")).upper(),
            "reason": str(parsed.get("reason", ""))[:240],
        }
    except Exception as e:
        log.debug(f"Grok judge error ${ticker}: {e}")
        return None


# ─── Orchestrator ─────────────────────────────────────────────────────────────

async def build_verdict(session: aiohttp.ClientSession, deployer_handle: str,
                        ticker: str, token_name: str = "", address: str = "") -> dict:
    """Gather SocialData facts + Grok live X investigation. Never raises."""
    ticker = (ticker or "?").lstrip("$")

    # SocialData: the hard facts
    deployer = await get_x_profile(session, deployer_handle) if deployer_handle else None
    project = await find_project_page(session, ticker, token_name)
    mentions = await get_notable_mentions(session, ticker)

    # Baseline heuristic (used if Grok is off/fails)
    score, label, emoji, reasons = _heuristic_score(deployer, project, mentions)
    grok_used = False

    # Grok: live X investigation + verdict (overrides heuristic)
    grok = await _grok_judge(session, ticker, token_name, address, deployer_handle, deployer, project, mentions)
    if grok:
        grok_used = True
        score = grok["score"]
        label = grok["label"]
        emoji = "🟢" if score >= 7 else "🟡" if score >= 4 else "🔴"
        if grok["reason"]:
            reasons = [grok["reason"]]

    return {
        "score": score, "label": label, "emoji": emoji, "reasons": reasons,
        "deployer": deployer, "project": project, "mentions": mentions,
        "grok": grok_used,
        "grok_searched": grok_used and GROK_LIVE_SEARCH,
    }


# ─── Formatting ───────────────────────────────────────────────────────────────

def _fmt_followers(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_verdict_block(v: dict) -> str:
    """Render the 🧠 VERDICT block appended to the signal message."""
    search_tag = " · 🔎X" if v.get("grok_searched") else ""
    lines = [f"🧠 <b>VERDICT: {v['emoji']} {v['label']}</b> ({v['score']}/10){search_tag}"]

    d = v.get("deployer")
    if d:
        bio = _esc((d.get("bio") or "").replace("\n", " ").strip())
        if len(bio) > 70:
            bio = bio[:67] + "..."
        verified = " ✔️" if d.get("verified") else ""
        bio_part = f' · "{bio}"' if bio else ""
        lines.append(f"├ 👤 <a href='https://x.com/{d['handle']}'>@{d['handle']}</a>{verified} · {_fmt_followers(d['followers'])}{bio_part}")
    else:
        lines.append("├ 👤 deployer: no X account found")

    p = v.get("project")
    if p:
        lines.append(f"├ 📄 Project: <a href='https://x.com/{p['handle']}'>@{p['handle']}</a> · {_fmt_followers(p['followers'])}")
    else:
        lines.append("├ 📄 No project page found by tools")

    m = v.get("mentions") or []
    if m:
        top = m[0]
        lines.append(f"└ 🐦 {len(m)} notable · top @{top['username']} ({_fmt_followers(top['followers'])})")
    else:
        lines.append("└ 🐦 No notable mentions")

    if v.get("grok") and v.get("reasons"):
        lines.append(f"<i>{_esc(v['reasons'][0])}</i>")

    return "\n".join(lines)
