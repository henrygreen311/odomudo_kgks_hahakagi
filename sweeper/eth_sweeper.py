import asyncio
import aiohttp
from web3 import Web3
from eth_account import Account
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from config import Config

try:
    from web3.middleware import geth_poa_middleware
except ImportError:
    try:
        from web3.middleware.geth_poa import geth_poa_middleware
    except ImportError:
        geth_poa_middleware = None

class EthSweeper:
    def __init__(self):
        self.cfg = Config

        # RPC for sending transactions (Infura – but only used when sweeping)
        self.w3 = Web3(Web3.HTTPProvider(self.cfg.ETH_RPC_URL))
        if not self.w3.is_connected():
            # Fallback to Cloudflare if Infura fails
            self.w3 = Web3(Web3.HTTPProvider("https://cloudflare-eth.com"))
            if not self.w3.is_connected():
                raise RuntimeError("Could not connect to any ETH RPC")

        if self.cfg.ETH_CHAIN_ID != 1 and geth_poa_middleware is not None:
            self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        seed_bytes = Bip39SeedGenerator(self.cfg.ETH_SOURCE_SEED).Generate()
        bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
        priv_key = bip44_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PrivateKey().Raw().ToBytes()
        self.source = Account.from_key(priv_key.hex())
        self.target = Web3.to_checksum_address(self.cfg.ETH_TARGET_ADDRESS)
        self.gas_limit = self.cfg.ETH_GAS_LIMIT
        self.gas_price_bump = self.cfg.ETH_GAS_PRICE_BUMP
        self.threshold_wei = int(self.cfg.ETH_SWEEP_THRESHOLD * 1e18)
        self.etherscan_key = self.cfg.ETHERSCAN_API_KEY

    async def get_balance(self) -> int:
        """Fetch balance using Etherscan API (free, 100k requests/day)."""
        url = f"https://api.etherscan.io/api?module=account&action=balance&address={self.source.address}&tag=latest&apikey={self.etherscan_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("status") == "1":
                        return int(data.get("result", "0"))
                    else:
                        # Fallback: use RPC if Etherscan fails
                        return self.w3.eth.get_balance(self.source.address)
        except Exception:
            # Fallback to RPC
            return self.w3.eth.get_balance(self.source.address)

    async def sweep(self) -> bool:
        balance_wei = await self.get_balance()
        if balance_wei == 0:
            return False

        if balance_wei < self.threshold_wei:
            return False

        try:
            gas_price = self.w3.eth.gas_price
        except Exception as e:
            print(f"  ⚠ Failed to fetch gas price: {e}")
            return False

        gas_price = int(gas_price * self.gas_price_bump)
        gas_cost = gas_price * self.gas_limit

        if balance_wei <= gas_cost:
            print("  ⚠ ETH balance too low to cover gas.")
            return False

        amount = balance_wei - gas_cost
        if amount <= 0:
            return False

        nonce = self.w3.eth.get_transaction_count(self.source.address)
        tx = {
            'to': self.target,
            'value': amount,
            'gas': self.gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
            'chainId': self.cfg.ETH_CHAIN_ID,
        }

        signed = self.source.sign_transaction(tx)

        try:
            raw_tx = signed.raw_transaction
        except AttributeError:
            raw_tx = signed.rawTransaction

        try:
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            print(f"  ✅ ETH sweep sent: {tx_hash.hex()}")
        except Exception as e:
            print(f"  ❌ ETH sweep failed: {e}")
            return False

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            print(f"  ✅ ETH sweep confirmed: {tx_hash.hex()}")
            return True
        else:
            print(f"  ❌ ETH sweep failed: {tx_hash.hex()}")
            return False