from __future__ import annotations

import aiohttp

from services.chains.evm import BaseEvmAdapter


def get_chain_adapter(chain: str, session: aiohttp.ClientSession, *, rpc_url: str):
    if chain == "base":
        return BaseEvmAdapter(session, rpc_url)
    raise ValueError(f"Unsupported chain adapter: {chain}")
