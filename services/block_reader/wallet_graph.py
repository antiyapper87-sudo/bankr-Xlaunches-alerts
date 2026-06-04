from __future__ import annotations


def cluster_key(chain: str, cluster_type: str, seed: str) -> str:
    return f"{chain}:{cluster_type}:{seed}".lower()
