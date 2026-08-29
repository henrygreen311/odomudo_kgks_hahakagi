#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import asyncio
import aiohttp
import json
import time
import random
import logging
import dropbox
from mnemonic import Mnemonic
from bip_utils import (
    Bip39SeedGenerator,
    Bip44,
    Bip44Coins,
    Bip44Changes,
    Bip44Conf,
)

Bip44Conf.ENABLE_UNSAFE_HDWALLET = True

# ------------------ CONFIG / CONSTANTS ------------------
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER_PATH", "/")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
ETH_API_FILE = "ETH_api.txt"
TRON_API_FILE = "TRON_api.txt"
ETH_RESPONSE_FILE = "ETH_scan_response.json"
TRON_RESPONSE_FILE = "TRON_scan_response.json"

MAX_CONCURRENT = 500
BATCH_WRITE_INTERVAL = 100
# Minimum number of API keys required (optional, just warn if less)
MIN_API_KEYS = 1

# ------------------ SILENT LOGGING ------------------
class NullHandler(logging.Handler):
    def emit(self, record):
        pass

logger = logging.getLogger("wallet_scanner")
logger.handlers = []
logger.addHandler(NullHandler())
logger.propagate = False
logger.setLevel(logging.CRITICAL)

logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

mnemo = Mnemonic("english")
api_call_counter = {"eth": 0, "tron": 0}
scanned_counter = 0

# ------------------ DROPBOX CLIENT FACTORY ------------------
def get_dropbox_client():
    if DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET:
        try:
            return dropbox.Dropbox(
                oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
                app_key=DROPBOX_APP_KEY,
                app_secret=DROPBOX_APP_SECRET,
            )
        except Exception as e:
            print(f"Failed to create Dropbox client with refresh token: {e}")
            sys.exit(1)
    elif DROPBOX_ACCESS_TOKEN:
        print("WARNING: Using short‑lived access token; it may expire.")
        return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
    else:
        print("ERROR: No Dropbox credentials found.")
        sys.exit(1)

# ------------------ API KEY ROTATING MANAGER (round-robin) ------------------
class RotatingBatchManager:
    def __init__(self, keys):
        self.keys = keys
        self.pointer = 0
        self.lock = asyncio.Lock()

    async def get_n_keys(self, n):
        """Return a list of n keys in round‑robin order."""
        keys = []
        async with self.lock:
            for _ in range(n):
                key = self.keys[self.pointer]
                self.pointer = (self.pointer + 1) % len(self.keys)
                keys.append(key)
        return keys

def read_api_keys(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            keys = [line.strip() for line in f if line.strip()]
        if len(keys) < MIN_API_KEYS:
            print(f"WARNING: Only {len(keys)} keys found in {path}. Minimum recommended: {MIN_API_KEYS}")
        if not keys:
            print(f"ERROR: No API keys in {path}.")
            return None
        return RotatingBatchManager(keys)
    except Exception as e:
        print(f"Error reading {path}: {e}", flush=True)
        return None

# ------------------ DERIVATION FUNCTIONS ------------------
def derive_eth_addresses(seed_phrase):
    try:
        seed_bytes = Bip39SeedGenerator(seed_phrase).Generate()
        bip44_m = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
        return [
            bip44_m.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        ]
    except Exception:
        return []

def derive_tron_addresses(seed_phrase):
    try:
        seed_bytes = Bip39SeedGenerator(seed_phrase).Generate()
        bip44_m = Bip44.FromSeed(seed_bytes, Bip44Coins.TRON)
        return [
            bip44_m.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        ]
    except Exception:
        return []

# ------------------ NETWORK / REQUESTS (with 1–3s delay) ------------------
async def robust_request(session, url, headers=None):
    # Add a random 1-3 second delay to avoid rate limiting
    await asyncio.sleep(random.uniform(1.0, 3.0))
    while True:
        try:
            async with session.get(url, headers=headers, timeout=30) as r:
                status = r.status
                text = await r.text()
                if status == 429:
                    # Rate limit: wait longer and retry
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                    continue
                if status != 200:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    continue
                try:
                    data = json.loads(text)
                except Exception:
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    continue
                return data
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(random.uniform(0.5, 1.5))

async def check_eth_balance(session, address, api_key):
    api_call_counter["eth"] += 1
    url = f"https://api.etherscan.io/v2/api?chainid=1&module=account&action=balance&address={address}&tag=latest&apikey={api_key}"
    data = await robust_request(session, url)
    balance = int(data.get("result", 0)) / 1e18 if data.get("status") == "1" else 0.0
    return balance, data

async def check_trx_account(session, address, api_key):
    api_call_counter["tron"] += 1
    url = f"https://api.trongrid.io/v1/accounts/{address}"
    headers = {"TRON-PRO-API-KEY": api_key}
    data = await robust_request(session, url, headers)
    accounts = data.get("data", []) if isinstance(data, dict) else []
    balance = accounts[0].get("balance", 0) / 1e6 if accounts else 0.0
    return balance, data

# ------------------ BATCH WRITER ------------------
class BatchWriter:
    def __init__(self, eth_file, tron_file, interval=BATCH_WRITE_INTERVAL):
        self.eth_file = eth_file
        self.tron_file = tron_file
        self.interval = interval
        self.eth_buffer = []
        self.tron_buffer = []
        self.eth_lock = asyncio.Lock()
        self.tron_lock = asyncio.Lock()
        self.counter = 0

    async def add(self, eth_entry, tron_entry):
        async with self.eth_lock:
            self.eth_buffer.append(eth_entry)
        async with self.tron_lock:
            self.tron_buffer.append(tron_entry)
        self.counter += 1
        if self.counter % self.interval == 0:
            await self.flush()

    async def flush(self):
        if self.eth_buffer:
            async with self.eth_lock:
                to_write = self.eth_buffer
                self.eth_buffer = []
            with open(self.eth_file, "a", encoding="utf-8") as f:
                for entry in to_write:
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        if self.tron_buffer:
            async with self.tron_lock:
                to_write = self.tron_buffer
                self.tron_buffer = []
            with open(self.tron_file, "a", encoding="utf-8") as f:
                for entry in to_write:
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")

# ------------------ SINGLE SEED SCAN (single request per chain) ------------------
async def scan_seed(seed, eth_key, tron_key, session, writer, eth_sem, tron_sem):
    async with eth_sem, tron_sem:
        eth_addresses = derive_eth_addresses(seed)
        tron_addresses = derive_tron_addresses(seed)

        # ETH – balance only
        eth_responses = []
        for addr in eth_addresses:
            balance, bal_resp = await check_eth_balance(session, addr, eth_key)
            eth_responses.append({
                "address": addr,
                "balance": balance,
                "balance_raw": bal_resp,
            })
        eth_entry = {"seed": seed, "eth": eth_responses, "timestamp": time.time()}

        # TRON – account info only
        tron_responses = []
        for addr in tron_addresses:
            balance, bal_resp = await check_trx_account(session, addr, tron_key)
            tron_responses.append({
                "address": addr,
                "balance": balance,
                "balance_raw": bal_resp,
            })
        tron_entry = {"seed": seed, "tron": tron_responses, "timestamp": time.time()}

        await writer.add(eth_entry, tron_entry)

        global scanned_counter
        scanned_counter += 1
        if scanned_counter % 1000 == 0:
            print(f"Scanned {scanned_counter} seeds so far...")

# ------------------ PROCESS ONE BATCH FILE ------------------
async def process_batch_file(dbx, file_metadata, eth_mgr, tron_mgr, session,
                             writer, eth_sem, tron_sem):
    file_name = file_metadata.name
    print(f"Processing file: {file_name}")

    try:
        _, res = dbx.files_download(file_metadata.path_display)
        content = res.content.decode("utf-8")
        seeds = [line.strip() for line in content.splitlines() if line.strip()]
    except Exception as e:
        print(f"Failed to download {file_name}: {e}")
        return

    if not seeds:
        print(f"File {file_name} is empty, deleting.")
        dbx.files_delete_v2(file_metadata.path_display)
        return

    print(f"File contains {len(seeds)} seeds. Scanning...")

    eth_keys = await eth_mgr.get_n_keys(len(seeds))
    tron_keys = await tron_mgr.get_n_keys(len(seeds))

    tasks = []
    for i, seed in enumerate(seeds):
        task = asyncio.create_task(
            scan_seed(seed, eth_keys[i], tron_keys[i], session, writer, eth_sem, tron_sem)
        )
        tasks.append(task)

    await asyncio.gather(*tasks)
    await writer.flush()

    # ---- Call the scanner to detect active wallets ----
    try:
        from src.scanner import process_scanner
        print("Calling scanner to detect active wallets...")
        process_scanner()
        print("Scanner finished.")
    except ImportError:
        print("WARNING: src.scanner not found, skipping active wallet detection.")
    except Exception as e:
        print(f"Scanner error: {e}")

    # ---- Now delete the batch file from Dropbox ----
    try:
        dbx.files_delete_v2(file_metadata.path_display)
        print(f"Deleted {file_name} from Dropbox.")
    except Exception as e:
        print(f"Failed to delete {file_name}: {e}")

# ------------------ MAIN LOOP ------------------
async def main():
    global scanned_counter
    scanned_counter = 0

    eth_mgr = read_api_keys(ETH_API_FILE)
    tron_mgr = read_api_keys(TRON_API_FILE)
    if not eth_mgr or not tron_mgr:
        print("ERROR: Missing API keys.")
        sys.exit(1)

    dbx = get_dropbox_client()

    eth_sem = asyncio.Semaphore(MAX_CONCURRENT)
    tron_sem = asyncio.Semaphore(MAX_CONCURRENT)
    writer = BatchWriter(ETH_RESPONSE_FILE, TRON_RESPONSE_FILE)

    try:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    entries = dbx.files_list_folder(DROPBOX_FOLDER)
                    files = [e for e in entries.entries
                             if isinstance(e, dropbox.files.FileMetadata) and e.name.startswith("seeds_")]

                    if not files:
                        print("No seed files found. Sleeping 30s...")
                        await asyncio.sleep(30)
                        continue

                    for file_meta in files:
                        await process_batch_file(
                            dbx, file_meta, eth_mgr, tron_mgr, session,
                            writer, eth_sem, tron_sem
                        )
                except dropbox.exceptions.AuthError as e:
                    print(f"Authentication error: {e}. The client should auto‑refresh; if this persists, check your refresh token.")
                    await asyncio.sleep(60)
                except dropbox.exceptions.ApiError as e:
                    print(f"Dropbox API error: {e}")
                    await asyncio.sleep(10)
                except Exception as e:
                    print(f"Unexpected error: {e}")
                    await asyncio.sleep(5)

    except KeyboardInterrupt:
        print("\nShutdown requested. Cleaning up...")

    print(f"Scanner finished. Total API calls - ETH: {api_call_counter['eth']}, TRON: {api_call_counter['tron']}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Graceful shutdown.")