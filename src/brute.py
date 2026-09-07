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
import io
from mnemonic import Mnemonic
from bip_utils import (
    Bip39SeedGenerator,
    Bip44,
    Bip44Coins,
    Bip44Changes,
    Bip44Conf,
)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from supabase import create_client

Bip44Conf.ENABLE_UNSAFE_HDWALLET = True

# ------------------ CONFIG / CONSTANTS ------------------
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
DRIVE_CREDENTIALS = os.getenv("DRIVE_CREDENTIALS")
DRIVE_TOKEN = os.getenv("DRIVE_TOKEN")
ETH_API_FILE = "ETH_api.txt"
TRON_API_FILE = "TRON_api.txt"
ETH_RESPONSE_FILE = "ETH_scan_response.json"
TRON_RESPONSE_FILE = "TRON_scan_response.json"

MAX_CONCURRENT = 500
BATCH_WRITE_INTERVAL = 100
MIN_API_KEYS = 1
PROGRESS_CHUNK_SIZE = 2000

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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

# ------------------ SUPABASE HELPERS FOR PROGRESS ------------------
def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_row_id():
    supabase = get_supabase()
    res = supabase.table("brute").select("id").limit(1).execute()
    if not res.data:
        raise RuntimeError("No row in brute table")
    return res.data[0]["id"]

def get_scan_progress(file_id):
    supabase = get_supabase()
    row_id = get_row_id()
    res = supabase.table("brute").select("scan_progress").eq("id", row_id).execute()
    if res.data and res.data[0].get("scan_progress"):
        return res.data[0]["scan_progress"].get(file_id, 0)
    return 0

def update_scan_progress(file_id, progress):
    supabase = get_supabase()
    row_id = get_row_id()
    res = supabase.table("brute").select("scan_progress").eq("id", row_id).execute()
    current = res.data[0].get("scan_progress", {}) if res.data else {}
    if not isinstance(current, dict):
        current = {}
    current[file_id] = progress
    supabase.table("brute").update({"scan_progress": current}).eq("id", row_id).execute()

def delete_scan_progress(file_id):
    supabase = get_supabase()
    row_id = get_row_id()
    res = supabase.table("brute").select("scan_progress").eq("id", row_id).execute()
    if res.data and res.data[0].get("scan_progress"):
        current = res.data[0]["scan_progress"]
        if file_id in current:
            del current[file_id]
            supabase.table("brute").update({"scan_progress": current}).eq("id", row_id).execute()

# ------------------ GOOGLE DRIVE SERVICE ------------------
def get_drive_service():
    if not DRIVE_CREDENTIALS or not DRIVE_TOKEN or not DRIVE_FOLDER_ID:
        raise RuntimeError("Missing Google Drive environment variables")

    token_info = json.loads(DRIVE_TOKEN)
    creds = Credentials.from_authorized_user_info(info=token_info, scopes=["https://www.googleapis.com/auth/drive.file"])
    service = build("drive", "v3", credentials=creds)
    return service

# ------------------ API KEY ROTATING MANAGER ------------------
class RotatingBatchManager:
    def __init__(self, keys):
        self.keys = keys
        self.pointer = 0
        self.lock = asyncio.Lock()

    async def get_n_keys(self, n):
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
            print(f"WARNING: Only {len(keys)} keys found in {path}")
        if not keys:
            print(f"ERROR: No API keys in {path}")
            return None
        return RotatingBatchManager(keys)
    except Exception as e:
        print(f"Error reading {path}: {e}")
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

# ------------------ NETWORK / REQUESTS (1s jitter) ------------------
async def robust_request(session, url, headers=None):
    # 1‑second jitter to spread requests evenly (0.5–1.5s)
    await asyncio.sleep(random.uniform(0.5, 0.8))
    while True:
        try:
            async with session.get(url, headers=headers, timeout=30) as r:
                status = r.status
                text = await r.text()
                if status == 429:
                    # Rate limit – wait longer and retry
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

# ------------------ SINGLE SEED SCAN ------------------
async def scan_seed(seed, eth_key, tron_key, session, writer, eth_sem, tron_sem):
    async with eth_sem, tron_sem:
        eth_addresses = derive_eth_addresses(seed)
        tron_addresses = derive_tron_addresses(seed)

        eth_responses = []
        for addr in eth_addresses:
            balance, bal_resp = await check_eth_balance(session, addr, eth_key)
            eth_responses.append({
                "address": addr,
                "balance": balance,
                "balance_raw": bal_resp,
            })
        eth_entry = {"seed": seed, "eth": eth_responses, "timestamp": time.time()}

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

# ------------------ PROCESS A CHUNK OF SEEDS ------------------
async def process_seed_chunk(seeds, eth_mgr, tron_mgr, session, writer,
                             eth_sem, tron_sem, start_offset, file_id, total_seeds):
    tasks = []
    for seed in seeds:
        eth_key = await eth_mgr.get_n_keys(1)
        tron_key = await tron_mgr.get_n_keys(1)
        task = asyncio.create_task(
            scan_seed(seed, eth_key[0], tron_key[0], session, writer, eth_sem, tron_sem)
        )
        tasks.append(task)

    await asyncio.gather(*tasks)
    await writer.flush()

    new_progress = start_offset + len(seeds)
    update_scan_progress(file_id, new_progress)

    global scanned_counter
    scanned_counter += len(seeds)
    print(f"Scanned {scanned_counter} seeds so far...")

# ------------------ PROCESS ONE BATCH FILE ------------------
async def process_batch_file(service, file_metadata, eth_mgr, tron_mgr, session,
                             writer, eth_sem, tron_sem):
    file_id = file_metadata["id"]
    file_name = file_metadata["name"]
    print(f"Processing file: {file_name}")

    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        content = fh.getvalue().decode("utf-8")
        seeds = [line.strip() for line in content.splitlines() if line.strip()]
    except Exception as e:
        print(f"Failed to download {file_name}: {e}")
        return

    if not seeds:
        print(f"File {file_name} is empty, deleting.")
        service.files().delete(fileId=file_id).execute()
        return

    total_seeds = len(seeds)
    progress = get_scan_progress(file_id)

    if progress >= total_seeds:
        print(f"File {file_name} already fully scanned. Deleting.")
        delete_scan_progress(file_id)
        service.files().delete(fileId=file_id).execute()
        return

    if progress > 0:
        print(f"Resuming from seed {progress}/{total_seeds}")
        seeds = seeds[progress:]

    print(f"Scanning {len(seeds)} remaining seeds...")

    for chunk_start in range(0, len(seeds), PROGRESS_CHUNK_SIZE):
        chunk_end = min(chunk_start + PROGRESS_CHUNK_SIZE, len(seeds))
        chunk = seeds[chunk_start:chunk_end]
        await process_seed_chunk(
            chunk, eth_mgr, tron_mgr, session, writer,
            eth_sem, tron_sem,
            progress + chunk_start, file_id, total_seeds
        )

    delete_scan_progress(file_id)

    try:
        from src.scanner import process_scanner
        print("Calling scanner to detect active wallets...")
        process_scanner()
        print("Scanner finished.")
    except ImportError:
        print("WARNING: src.scanner not found, skipping active wallet detection.")
    except Exception as e:
        print(f"Scanner error: {e}")

    retries = 0
    while True:
        try:
            service.files().delete(fileId=file_id).execute()
            print(f"Deleted {file_name} from Google Drive.")
            break
        except Exception as e:
            retries += 1
            wait = min(2 ** retries, 60)
            print(f"Delete attempt {retries} failed for {file_name}: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)

# ------------------ MAIN LOOP ------------------
async def main():
    global scanned_counter
    scanned_counter = 0

    eth_mgr = read_api_keys(ETH_API_FILE)
    tron_mgr = read_api_keys(TRON_API_FILE)
    if not eth_mgr or not tron_mgr:
        print("ERROR: Missing API keys.")
        sys.exit(1)

    service = get_drive_service()

    eth_sem = asyncio.Semaphore(MAX_CONCURRENT)
    tron_sem = asyncio.Semaphore(MAX_CONCURRENT)
    writer = BatchWriter(ETH_RESPONSE_FILE, TRON_RESPONSE_FILE)

    try:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    results = service.files().list(
                        q=f"'{DRIVE_FOLDER_ID}' in parents and name contains 'seeds_'",
                        fields="files(id, name)"
                    ).execute()
                    files = results.get("files", [])

                    if not files:
                        print("No seed files found. Exiting.")
                        break

                    files.sort(key=lambda x: x["name"])
                    print(f"Found {len(files)} seed files. Processing...")

                    for file_meta in files:
                        await process_batch_file(
                            service, file_meta, eth_mgr, tron_mgr, session,
                            writer, eth_sem, tron_sem
                        )

                except Exception as e:
                    print(f"Error in main loop: {e}")
                    await asyncio.sleep(10)

    except KeyboardInterrupt:
        print("\nShutdown requested. Cleaning up...")

    print(f"Scanner finished. Total API calls - ETH: {api_call_counter['eth']}, TRON: {api_call_counter['tron']}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Graceful shutdown.")