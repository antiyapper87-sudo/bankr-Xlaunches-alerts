# Private Nitter Runbook

Private Nitter is an optional local-only X fallback backend for research.

## VM Layout

- Nitter URL: `http://127.0.0.1:18080`
- Nitter config: `/opt/nitter-private/nitter.conf`
- X session file: `/opt/nitter-private/sessions.jsonl`
- Redis for Nitter only: `127.0.0.1:6380`

Do not commit `sessions.jsonl`. It contains X session credentials.

## Services

```bash
systemctl status nitter-private.service
systemctl status nitter-redis.service
systemctl status nitter-private-firewall.service
```

Restart:

```bash
systemctl restart nitter-redis.service nitter-private.service
```

Logs:

```bash
journalctl -u nitter-private.service --since "10 minutes ago" --no-pager
```

## Health Check

```bash
curl -I http://127.0.0.1:18080/elonmusk/rss
curl "http://127.0.0.1:18080/search/rss?f=tweets&q=%24CUE"
```

Expected: `200 application/rss+xml`.

## Security

Nitter is intended to be reachable only from localhost.

Firewall rules:

```bash
iptables -S INPUT | grep -E "18080|6380"
```

Expected:

```text
-A INPUT -i lo -p tcp -m multiport --dports 18080,6380 -j ACCEPT
-A INPUT -p tcp -m multiport --dports 18080,6380 -j DROP
```

The firewall service reapplies these rules after reboot.

## Bot Integration Contract

Use Nitter only as fallback discovery:

1. Try SocialData first.
2. If SocialData fails or returns empty, query Nitter RSS.
3. Extract tweet ids/links from RSS.
4. Enrich/verify through SocialData before counting tweets in social confirmation.

Unverified Nitter-only tweets must not count toward the `RESEARCH_MIN_QUALIFIED_TWEETS`
rule and must not increase social score.
