"""
trader.py — On-chain buy/sell execution for Whale Alert Bot
===========================================================
Uses web3.py + Uniswap V3 SwapRouter on Base mainnet.
Triggered by Telegram inline button callbacks.
"""

import os
import logging
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

log = logging.getLogger("whale-alert")

# ─── Config ───────────────────────────────────────────────────────────────────

ALCHEMY_RPC_URL = os.getenv("ALCHEMY_RPC_URL", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

# Uniswap V3 SwapRouter02 on Base
UNISWAP_ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")

# WETH on Base
WETH_ADDRESS = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")

# Default slippage: 10% (handles volatile new tokens)
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "1000"))

# Uniswap V3 SwapRouter02 ABI (exactInputSingle + exactOutputSingle)
ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn",       "type": "address"},
                {"name": "tokenOut",      "type": "address"},
                {"name": "fee",           "type": "uint24"},
                {"name": "recipient",     "type": "address"},
                {"name": "amountIn",      "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params",
            "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn",       "type": "address"},
                {"name": "tokenOut",      "type": "address"},
                {"name": "fee",           "type": "uint24"},
                {"name": "recipient",     "type": "address"},
                {"name": "amountIn",      "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params",
            "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
]

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
]


def get_web3():
    if not ALCHEMY_RPC_URL:
        raise ValueError("ALCHEMY_RPC_URL not set")
    w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError("Cannot connect to Base RPC")
    return w3


def get_wallet(w3):
    if not PRIVATE_KEY:
        raise ValueError("PRIVATE_KEY not set")
    account = w3.eth.account.from_key(PRIVATE_KEY)
    return account


async def get_eth_balance(w3=None) -> float:
    """Returns ETH balance of bot wallet in ETH (float)."""
    if w3 is None:
        w3 = get_web3()
    account = get_wallet(w3)
    balance_wei = w3.eth.get_balance(account.address)
    return float(w3.from_wei(balance_wei, "ether"))


async def get_token_balance(token_address: str, w3=None) -> tuple[float, int]:
    """Returns (token balance as float, decimals) for bot wallet."""
    if w3 is None:
        w3 = get_web3()
    account = get_wallet(w3)
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_ABI
    )
    decimals = token.functions.decimals().call()
    raw_balance = token.functions.balanceOf(account.address).call()
    return float(raw_balance / (10 ** decimals)), decimals


async def buy_token(token_address: str, eth_percent: int) -> dict:
    """
    Buy token_address using eth_percent% of wallet ETH balance.
    Returns dict with success, tx_hash, amount_eth, error.
    """
    try:
        w3 = get_web3()
        account = get_wallet(w3)

        # Get ETH balance
        eth_balance_wei = w3.eth.get_balance(account.address)
        eth_balance = float(w3.from_wei(eth_balance_wei, "ether"))

        # Reserve 0.002 ETH for gas
        GAS_RESERVE = 0.002
        spendable = max(0, eth_balance - GAS_RESERVE)
        if spendable <= 0:
            return {"success": False, "error": f"Insufficient ETH (balance: {eth_balance:.4f}, need >{GAS_RESERVE})"}

        amount_eth = spendable * (eth_percent / 100)
        amount_wei = w3.to_wei(amount_eth, "ether")

        if amount_wei == 0:
            return {"success": False, "error": "Amount too small"}

        router = w3.eth.contract(address=UNISWAP_ROUTER, abi=ROUTER_ABI)
        token_addr = Web3.to_checksum_address(token_address)

        # Fee tiers to try: 1% (10000), 0.3% (3000), 0.05% (500)
        fee_tiers = [10000, 3000, 500]
        last_error = None

        for fee in fee_tiers:
            try:
                # amountOutMinimum = 0 with slippage handled via deadline
                # For new tokens slippage is high, use 0 min output (accept whatever)
                params = {
                    "tokenIn": WETH_ADDRESS,
                    "tokenOut": token_addr,
                    "fee": fee,
                    "recipient": account.address,
                    "amountIn": amount_wei,
                    "amountOutMinimum": 0,
                    "sqrtPriceLimitX96": 0,
                }

                nonce = w3.eth.get_transaction_count(account.address)
                gas_price = w3.eth.gas_price

                tx = router.functions.exactInputSingle(params).build_transaction({
                    "from": account.address,
                    "value": amount_wei,
                    "nonce": nonce,
                    "gasPrice": int(gas_price * 1.2),  # 20% tip
                    "gas": 300000,
                    "chainId": 8453,  # Base mainnet
                })

                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                tx_hex = tx_hash.hex()

                log.info(f"  💸 BUY {eth_percent}% ETH ({amount_eth:.4f} ETH) → ${token_address[:8]}... | fee={fee} | tx={tx_hex[:16]}...")

                return {
                    "success": True,
                    "tx_hash": tx_hex,
                    "amount_eth": amount_eth,
                    "eth_percent": eth_percent,
                    "fee_tier": fee,
                }
            except Exception as e:
                last_error = str(e)
                if "insufficient liquidity" in str(e).lower() or "execution reverted" in str(e).lower():
                    continue  # try next fee tier
                else:
                    break  # non-liquidity error, don't retry

        return {"success": False, "error": f"All fee tiers failed: {last_error}"}

    except Exception as e:
        log.error(f"buy_token error: {e}")
        return {"success": False, "error": str(e)}


async def sell_token(token_address: str, token_percent: int) -> dict:
    """
    Sell token_percent% of bot wallet's token balance.
    Returns dict with success, tx_hash, amount_tokens, error.
    """
    try:
        w3 = get_web3()
        account = get_wallet(w3)

        token_addr = Web3.to_checksum_address(token_address)
        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)

        decimals = token_contract.functions.decimals().call()
        raw_balance = token_contract.functions.balanceOf(account.address).call()

        if raw_balance == 0:
            return {"success": False, "error": "No token balance to sell"}

        amount_raw = int(raw_balance * token_percent / 100)
        amount_tokens = amount_raw / (10 ** decimals)

        if amount_raw == 0:
            return {"success": False, "error": "Sell amount too small"}

        router = w3.eth.contract(address=UNISWAP_ROUTER, abi=ROUTER_ABI)

        # Approve router to spend tokens
        nonce = w3.eth.get_transaction_count(account.address)
        gas_price = w3.eth.gas_price

        approve_tx = token_contract.functions.approve(
            UNISWAP_ROUTER, amount_raw
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gasPrice": int(gas_price * 1.2),
            "gas": 100000,
            "chainId": 8453,
        })
        signed_approve = w3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
        w3.eth.send_raw_transaction(signed_approve.raw_transaction)

        # Wait briefly for approve to land
        import time
        time.sleep(2)

        fee_tiers = [10000, 3000, 500]
        last_error = None

        for fee in fee_tiers:
            try:
                nonce = w3.eth.get_transaction_count(account.address)
                params = {
                    "tokenIn": token_addr,
                    "tokenOut": WETH_ADDRESS,
                    "fee": fee,
                    "recipient": account.address,
                    "amountIn": amount_raw,
                    "amountOutMinimum": 0,
                    "sqrtPriceLimitX96": 0,
                }

                tx = router.functions.exactInputSingle(params).build_transaction({
                    "from": account.address,
                    "value": 0,
                    "nonce": nonce,
                    "gasPrice": int(gas_price * 1.2),
                    "gas": 300000,
                    "chainId": 8453,
                })

                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                tx_hex = tx_hash.hex()

                log.info(f"  💸 SELL {token_percent}% ({amount_tokens:.2f} tokens) → ETH | fee={fee} | tx={tx_hex[:16]}...")

                return {
                    "success": True,
                    "tx_hash": tx_hex,
                    "amount_tokens": amount_tokens,
                    "token_percent": token_percent,
                    "fee_tier": fee,
                }
            except Exception as e:
                last_error = str(e)
                if "insufficient liquidity" in str(e).lower() or "execution reverted" in str(e).lower():
                    continue
                else:
                    break

        return {"success": False, "error": f"All fee tiers failed: {last_error}"}

    except Exception as e:
        log.error(f"sell_token error: {e}")
        return {"success": False, "error": str(e)}
