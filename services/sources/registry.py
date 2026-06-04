from __future__ import annotations

from services.sources.bankr import BankrAdapter
from services.sources.clanker import ClankerAdapter
from services.sources.coingecko import CoinGeckoAdapter
from services.sources.dexscreener import DexScreenerAdapter
from services.sources.virtuals import VirtualsAdapter
from services.sources.zora_coins import ZoraCoinsAdapter


def base_source_adapters():
    return [
        BankrAdapter(),
        ClankerAdapter(),
        VirtualsAdapter(),
        DexScreenerAdapter(),
        CoinGeckoAdapter(),
        ZoraCoinsAdapter(),
    ]
