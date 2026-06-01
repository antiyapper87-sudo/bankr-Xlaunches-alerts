"""
verdict.py — Auto-research & legitimacy scoring for Whale Alert Bot
===================================================================
Runs AFTER a signal is sent (as a background task) and edits the
Telegram message in-place to append a 🧠 VERDICT block — so signal
latency is UNCHANGED. The signal lands instantly; the verdict fills
in ~3-8s later by editing the same message.

Codifies the manual X-research workflow:
  1. Deployer credibility — followers + bio (role / project affiliation)
  2. Project page         — is there an X account named after the ticker?
  3. Notable mentions     — 10K+ follower accounts talking about $TICKER

An optional Grok (xAI) pass synthesizes the final verdict over the data
gathered from SocialData. Falls back to a pure heuristic score if Grok
is disabled, has no key, or the call fails.

Self-contained: reads its own env vars, no circular imports with main.py.

Env vars:
  SOCIALDATA_API_KEY   — required (already set for the bot)
  XAI_API_KEY          — optional, enables the Grok verdict pass
  GROK_MODEL           — default "grok-4" (set to whatever your MCP uses)
  USE_GROK_VERDICT     — "true"/"false", default "true"
  VERDICT_MIN_FOLLOWERS — notable-mention follower floor, default 10000
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
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4")
USE_GROK_VERDICT = os.getenv("USE_GROK_VERDICT", "true").lower() == "true"

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
    """Fetch an X profile: followers + bio + name + verified. One SocialData call.

    The bio is FREE here — it comes back in the same response the bot already
    uses for follower counts; we just stop throwing it away.
    """
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
                log.debug(f"get_x_profile @{username} → {resp.status}")
    except Exception as e:
        log.debug(f"get_x_profile error @{username}: {e}")
        return None

    _profile_cache[username] = profile
    return profile


# ─── SocialData: project-page detection ───────────────────────────────────────

async def find_project_page(session: aiohttp.ClientSession, ticker: str, token_name: str = "") -> dict | None:
    """Probe for a dedicated project X account named after the ticker / name.

    Most legit launches (e.g. GITLAWB → @gitlawb) use a handle == ticker, so a
    direct handle probe is cheaper and more reliable than a fuzzy user search.
    """
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


# ─── Heuristic scoring ────────────────────────────────────────────────────────

def _bio_project_signal(bio: str) -> bool:
    """Does the bio suggest a real builder / project affiliation?"""
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


# ─── Grok (xAI) verdict pass ──────────────────────────────────────────────────

async def _grok_judge(session: aiohttp.ClientSession, ticker: str, name: str,
                      deployer: dict | None, project: dict | None, mentions: list) -> dict | None:
    """Synthesize a verdict over the gathered data. Returns None if disabled/fails."""
    if not (USE_GROK_VERDICT and XAI_API_KEY):
        return None
    try:
        payload_data = {
            "ticker": ticker,
            "token_name": name,
            "deployer": deployer or {},
            "project_page": project or {},
            "notable_mentions": [
                {"handle": m["username"], "followers": m["followers"]} for m in mentions
            ],
        }
        system = (
            "You are a crypto launch analyst screening new Base token launches for a fund. "
            "Given deployer and project data, decide whether this is a low-effort spam launch "
            "or a credible one. Credible signals: solid/large deployer following, a deployer who "
            "is a real builder working on a project (per bio), or a dedicated project X account. "
            "Return ONLY compact JSON: "
            "{\"score\": <0-10 number>, \"label\": \"SOLID|MID|LIKELY SPAM\", "
            "\"reason\": \"one short sentence\"}. No prose, no markdown fences."
        )
        body = {
            "model": GROK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload_data)},
            ],
            "temperature": 0.1,
            "max_tokens": 200,
        }
        headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
        async with session.post(
            XAI_API_URL, json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.warning(f"  ⚠️ Grok verdict {resp.status} for ${ticker}")
                return None
            data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        return {
            "score": round(float(parsed.get("score", 0)), 1),
            "label": str(parsed.get("label", "MID")).upper(),
            "reason": str(parsed.get("reason", ""))[:160],
        }
    except Exception as e:
        log.debug(f"Grok judge error ${ticker}: {e}")
        return None


# ─── Orchestrator ─────────────────────────────────────────────────────────────

async def build_verdict(session: aiohttp.ClientSession, deployer_handle: str,
                        ticker: str, token_name: str = "") -> dict:
    """Gather data + score. Never raises; always returns a usable dict."""
    ticker = (ticker or "?").lstrip("$")
    deployer = await get_x_profile(session, deployer_handle) if deployer_handle else None
    project = await find_project_page(session, ticker, token_name)
    mentions = await get_notable_mentions(session, ticker)

    score, label, emoji, reasons = _heuristic_score(deployer, project, mentions)

    grok = await _grok_judge(session, ticker, token_name, deployer, project, mentions)
    if grok:
        score = grok["score"]
        label = grok["label"]
        emoji = "🟢" if score >= 7 else "🟡" if score >= 4 else "🔴"
        if grok["reason"]:
            reasons = [grok["reason"]]

    return {
        "score": score, "label": label, "emoji": emoji, "reasons": reasons,
        "deployer": deployer, "project": project, "mentions": mentions,
        "grok": grok is not None,
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
    lines = [f"🧠 <b>VERDICT: {v['emoji']} {v['label']}</b> ({v['score']}/10)"]

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
        lines.append("├ 📄 No project page found")

    m = v.get("mentions") or []
    if m:
        top = m[0]
        lines.append(f"└ 🐦 {len(m)} notable · top @{top['username']} ({_fmt_followers(top['followers'])})")
    else:
        lines.append("└ 🐦 No notable mentions")

    if v.get("grok") and v.get("reasons"):
        lines.append(f"<i>{_esc(v['reasons'][0])}</i>")

    return "\n".join(lines)
