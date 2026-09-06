import asyncio
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import Message
from solders.transaction import Transaction
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from config import Config

class SolSweeper:
    def __init__(self):
        self.cfg = Config
        self.client = AsyncClient("https://api.mainnet-beta.solana.com")

        seed_bytes = Bip39SeedGenerator(self.cfg.SOL_SOURCE_SEED).Generate()
        bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)
        priv_key_bytes = bip44_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PrivateKey().Raw().ToBytes()
        self.source = Keypair.from_seed(priv_key_bytes)
        self.target = Pubkey.from_string(self.cfg.SOL_TARGET_ADDRESS)
        self.threshold_lamports = int(self.cfg.SOL_SWEEP_THRESHOLD * 1e9)

    async def get_balance(self) -> int:
        try:
            resp = await self.client.get_balance(self.source.pubkey(), commitment=Confirmed)
            return resp.value
        except Exception:
            return 0

    async def sweep(self) -> bool:
        balance_lamports = await self.get_balance()
        if balance_lamports == 0:
            return False

        if balance_lamports < self.threshold_lamports:
            return False

        fee_estimate = 5000
        if balance_lamports <= fee_estimate:
            print("  ⚠ SOL balance too low to cover fee.")
            return False

        amount = balance_lamports - fee_estimate

        ix = transfer(TransferParams(
            from_pubkey=self.source.pubkey(),
            to_pubkey=self.target,
            lamports=amount
        ))

        blockhash_resp = await self.client.get_latest_blockhash()
        recent_blockhash = blockhash_resp.value.blockhash

        msg = Message.new_with_blockhash(
            instructions=[ix],
            payer=self.source.pubkey(),
            blockhash=recent_blockhash,
        )

        tx = Transaction.new_unsigned(msg)
        # Sign in place (returns None)
        tx.sign([self.source], recent_blockhash)

        opts = TxOpts(skip_confirmation=False, max_retries=3)
        resp = await self.client.send_transaction(tx, opts=opts)
        txid = resp.value
        print(f"  ✅ SOL sweep sent: {txid}")

        conf = await self.client.confirm_transaction(txid, commitment=Confirmed)
        if conf.value.confirmationStatus in ('confirmed', 'finalized'):
            print(f"  ✅ SOL sweep confirmed: {txid}")
            return True
        else:
            print(f"  ❌ SOL sweep failed: {txid}")
            return False