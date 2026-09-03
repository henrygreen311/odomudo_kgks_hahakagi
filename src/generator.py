#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import math
import signal
import time
import random
import json
import io
from concurrent.futures import ProcessPoolExecutor, as_completed
from mnemonic import Mnemonic
from supabase import create_client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ----------------------------------------------------------------------
# Environment & Constants
# ----------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DRIVE_CREDENTIALS = os.getenv("DRIVE_CREDENTIALS")
DRIVE_TOKEN = os.getenv("DRIVE_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# Process this many permutation indexes per output file
PERMUTATIONS_PER_FILE = 1_000_000

# Number of workers (processes) – increased to 40
NUM_WORKERS = 40

# Upload files in batches of this size
UPLOAD_BATCH_SIZE = 5

# Database column names
PROGRESS_COLUMN = "generation_progress"
PREVIOUS_SEED_COLUMN = "previous_seed_phrases"

stop_event = None

# ----------------------------------------------------------------------
# Supabase helpers
# ----------------------------------------------------------------------
def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_row_id():
    supabase = get_supabase()
    res = supabase.table("brute").select("id").limit(1).execute()
    if not res.data:
        raise RuntimeError("No row found in 'brute' table.")
    return res.data[0]["id"]

def get_progress():
    supabase = get_supabase()
    res = supabase.table("brute").select(PROGRESS_COLUMN).limit(1).execute()
    return res.data[0].get(PROGRESS_COLUMN, 0) if res.data else 0

def update_progress(increment, total_perms=None):
    """Atomically add `increment` to progress, but never exceed total_perms."""
    supabase = get_supabase()
    row_id = get_row_id()
    try:
        if total_perms is not None:
            supabase.rpc("increment_progress", {"inc": increment}).execute()
            current = supabase.table("brute").select(PROGRESS_COLUMN).eq("id", row_id).execute()
            if current.data and current.data[0][PROGRESS_COLUMN] > total_perms:
                supabase.table("brute").update({PROGRESS_COLUMN: total_perms}).eq("id", row_id).execute()
        else:
            supabase.rpc("increment_progress", {"inc": increment}).execute()
    except Exception:
        # Fallback read-modify-write with cap
        try:
            current = supabase.table("brute").select(PROGRESS_COLUMN).eq("id", row_id).execute()
            if current.data:
                new_value = current.data[0][PROGRESS_COLUMN] + increment
                if total_perms is not None and new_value > total_perms:
                    new_value = total_perms
                supabase.table("brute").update({PROGRESS_COLUMN: new_value}).eq("id", row_id).execute()
        except Exception as e:
            print(f"Progress update failed: {e}")

def set_progress(value):
    supabase = get_supabase()
    row_id = get_row_id()
    try:
        supabase.table("brute").update({PROGRESS_COLUMN: value}).eq("id", row_id).execute()
    except Exception as e:
        print(f"Failed to set progress: {e}")

def get_seed_phrases():
    supabase = get_supabase()
    res = supabase.table("brute").select("seed_phrases").limit(1).execute()
    if not res.data:
        raise RuntimeError("No row found in 'brute' table.")
    seed_phrases = res.data[0].get("seed_phrases")
    if not seed_phrases:
        # Fallback to local file
        try:
            with open("seed_phrases.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
            words = content.split()
            if len(words) != 12:
                raise ValueError("Need exactly 12 words")
            return words
        except FileNotFoundError:
            raise RuntimeError("seed_phrases column is empty and seed_phrases.txt not found.")
    words = seed_phrases.strip().split()
    if len(words) != 12:
        raise ValueError(f"seed_phrases must contain exactly 12 words, got {len(words)}")
    return words

def get_previous_seed_phrases():
    supabase = get_supabase()
    res = supabase.table("brute").select(PREVIOUS_SEED_COLUMN).limit(1).execute()
    if not res.data:
        raise RuntimeError("No row found in 'brute' table.")
    return res.data[0].get(PREVIOUS_SEED_COLUMN)

def set_previous_seed_phrases(seed_str):
    supabase = get_supabase()
    row_id = get_row_id()
    try:
        supabase.table("brute").update({PREVIOUS_SEED_COLUMN: seed_str}).eq("id", row_id).execute()
    except Exception as e:
        print(f"Failed to set previous seed phrases: {e}")

# ----------------------------------------------------------------------
# Google Drive helpers
# ----------------------------------------------------------------------
def get_drive_service():
    if not DRIVE_CREDENTIALS or not DRIVE_TOKEN or not DRIVE_FOLDER_ID:
        raise RuntimeError("Missing Google Drive environment variables")
    token_info = json.loads(DRIVE_TOKEN)
    creds = Credentials.from_authorized_user_info(info=token_info, scopes=["https://www.googleapis.com/auth/drive.file"])
    return build("drive", "v3", credentials=creds)

def upload_file_with_retry(service, content, filename, folder_id, max_retries=10):
    retries = 0
    while retries < max_retries:
        try:
            file_metadata = {"name": filename, "parents": [folder_id]}
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode("utf-8")),
                mimetype="text/plain",
                resumable=True
            )
            service.files().create(body=file_metadata, media_body=media, fields="id").execute()
            return True
        except Exception as e:
            if "rateLimitExceeded" in str(e) or "userRateLimitExceeded" in str(e) or "quotaExceeded" in str(e):
                wait = 2 ** retries + random.uniform(0, 1)
                print(f"Rate limit hit, retrying in {wait:.2f}s...")
                time.sleep(wait)
                retries += 1
                continue
            else:
                print(f"Upload error: {e}")
                retries += 1
                time.sleep(2 ** retries)
                continue
    return False

# ----------------------------------------------------------------------
# Worker – processes a fixed number of permutations per file
# ----------------------------------------------------------------------
def worker(start_idx, count, worker_id, run_id, stop_event, seed_words, total_perms):
    service = get_drive_service()
    folder_id = DRIVE_FOLDER_ID
    words = seed_words[:]
    mnemo = Mnemonic("english")

    current = start_idx
    remaining = count
    pending = []  # (content, filename, chunk_size)

    while remaining > 0 and not stop_event.is_set():
        chunk_size = min(remaining, PERMUTATIONS_PER_FILE)
        chunk_start = current
        chunk_end = chunk_start + chunk_size

        # --- Process the chunk ---
        valid_seeds = []
        for idx in range(chunk_start, chunk_end):
            arr = words[:]
            k = idx
            perm = []
            for j in range(12, 0, -1):
                fact = math.factorial(j - 1)
                pos = k // fact
                k %= fact
                perm.append(arr.pop(pos))
            mnemonic = ' '.join(perm)
            if mnemo.check(mnemonic):
                valid_seeds.append(mnemonic)

        # ---- Prepare or skip ----
        if valid_seeds:
            file_counter = chunk_start // PERMUTATIONS_PER_FILE + 1
            filename = f"seeds_{run_id}_w{worker_id}_{file_counter:08d}_{len(valid_seeds)}.txt"
            content = "\n".join(valid_seeds)
            pending.append((content, filename, chunk_size))
            print(f"[Worker {worker_id}] Chunk [{chunk_start:,} - {chunk_end:,}] → {len(valid_seeds):,} seeds, file ready.")
        else:
            update_progress(chunk_size, total_perms)
            print(f"[Worker {worker_id}] Chunk [{chunk_start:,} - {chunk_end:,}] → no seeds, progress +{chunk_size:,}")

        current += chunk_size
        remaining -= chunk_size

        # ---- Upload batch if full ----
        if len(pending) >= UPLOAD_BATCH_SIZE:
            total_uploaded = 0
            total_increment = 0
            print(f"[Worker {worker_id}] Uploading batch of {len(pending)} files...")
            for content, fname, csize in pending:
                success = upload_file_with_retry(service, content, fname, folder_id)
                if success:
                    total_uploaded += 1
                    total_increment += csize
                else:
                    # indefinite retry
                    while not upload_file_with_retry(service, content, fname, folder_id, max_retries=100):
                        time.sleep(5)
                    total_uploaded += 1
                    total_increment += csize
            if total_increment > 0:
                update_progress(total_increment, total_perms)
                print(f"[Worker {worker_id}] Batch uploaded – progress +{total_increment:,} ({total_uploaded} files)")
            pending.clear()

    # ---- Final flush ----
    if pending and not stop_event.is_set():
        total_increment = 0
        print(f"[Worker {worker_id}] Uploading final {len(pending)} files...")
        for content, fname, csize in pending:
            success = upload_file_with_retry(service, content, fname, folder_id)
            if success:
                total_increment += csize
            else:
                while not upload_file_with_retry(service, content, fname, folder_id, max_retries=100):
                    time.sleep(5)
                total_increment += csize
        if total_increment > 0:
            update_progress(total_increment, total_perms)
            print(f"[Worker {worker_id}] Final batch uploaded – progress +{total_increment:,}")

    print(f"[Worker {worker_id}] Finished. Processed {count:,} indices.")

# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
def main():
    global stop_event

    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: Supabase credentials missing.")
        sys.exit(1)
    if not (DRIVE_CREDENTIALS and DRIVE_TOKEN and DRIVE_FOLDER_ID):
        print("ERROR: Missing Google Drive environment variables.")
        sys.exit(1)

    try:
        seed_words = get_seed_phrases()
        current_seed_str = ' '.join(seed_words)
        print(f"Current seed phrases: {current_seed_str}")
    except Exception as e:
        print(f"ERROR: Could not load seed phrases: {e}")
        sys.exit(1)

    total_perms = math.factorial(len(seed_words))
    previous_seed_str = get_previous_seed_phrases()
    progress = get_progress()

    if progress > total_perms:
        print(f"WARNING: Progress ({progress}) exceeds total permutations ({total_perms}). Resetting to 0.")
        progress = 0
        set_progress(0)

    if previous_seed_str:
        if current_seed_str == previous_seed_str:
            if progress >= total_perms:
                print("\n" + "="*60)
                print("This seed phrases has been completed.")
                print("Update a new seed phrases when brute.py has done scanning all the valid seed phrases.")
                print("="*60 + "\n")
                sys.exit(0)
            else:
                print(f"Resuming from progress: {progress:,} / {total_perms:,}")
        else:
            print(f"New seed phrases detected. Resetting progress.")
            progress = 0
            set_progress(0)
            set_previous_seed_phrases(current_seed_str)
    else:
        print("First run detected. Setting previous seed phrases and starting from 0.")
        progress = 0
        set_progress(0)
        set_previous_seed_phrases(current_seed_str)

    remaining = total_perms - progress
    if remaining <= 0:
        print("All permutations already processed.")
        sys.exit(0)

    print(f"Total permutations: {total_perms:,}, already processed: {progress:,}, remaining: {remaining:,}")

    # Distribute work among workers
    chunk_size = remaining // NUM_WORKERS
    remainder = remaining % NUM_WORKERS
    tasks = []
    start = progress
    for w in range(NUM_WORKERS):
        count = chunk_size + (1 if w < remainder else 0)
        if count == 0:
            continue
        tasks.append((start, count, w + 1))
        start += count

    run_id = str(int(time.time()))

    import multiprocessing as mp
    manager = mp.Manager()
    stop_event = manager.Event()

    def signal_handler(sig, frame):
        print("\nInterrupt received! Setting stop event...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)

    try:
        with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = [executor.submit(worker, start, count, wid, run_id, stop_event, seed_words, total_perms)
                       for (start, count, wid) in tasks]

            try:
                for future in as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                print("Main process interrupted, waiting for workers to finish...")
                for future in futures:
                    try:
                        future.result(timeout=10)
                    except Exception:
                        pass
                final_progress = get_progress()
                if final_progress < total_perms:
                    print(f"Generation interrupted. Final progress: {final_progress:,} / {total_perms:,}")
                    sys.exit(1)
                else:
                    sys.exit(0)
    except Exception as e:
        print(f"Fatal error in generator: {e}")
        sys.exit(1)

    final_progress = get_progress()
    if final_progress >= total_perms:
        print("Generation completed!")
        sys.exit(0)
    else:
        print(f"Generation incomplete. Final progress: {final_progress:,} / {total_perms:,}")
        sys.exit(1)

if __name__ == "__main__":
    main()