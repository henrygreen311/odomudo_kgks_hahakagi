#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import math
import signal
import time
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
import dropbox
from mnemonic import Mnemonic
from supabase import create_client

# ----------------------------------------------------------------------
# Environment & Constants
# ----------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_FOLDER_PATH = os.getenv("DROPBOX_FOLDER_PATH", "/")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

# Process this many permutation indexes per output file
PERMUTATIONS_PER_FILE = 1_000_000

# Number of workers (processes)
NUM_WORKERS = 20

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
# Dropbox client
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# Upload helper with indefinite retry
# ----------------------------------------------------------------------
def upload_with_retry(dbx, content, path, max_retries=10):
    retries = 0
    while retries < max_retries:
        try:
            dbx.files_upload(content.encode('utf-8'), path,
                             mode=dropbox.files.WriteMode.overwrite,
                             mute=True)
            return True
        except dropbox.exceptions.ApiError as e:
            if e.error.is_path() and e.error.get_path().is_conflict():
                print(f"Upload conflict for {path}, skipping.")
                return True
            elif e.error.is_rate_limit():
                wait = 2 ** retries + random.uniform(0, 1)
                print(f"Rate limit hit for {path}, retrying in {wait:.2f}s...")
                time.sleep(wait)
                retries += 1
                continue
            else:
                print(f"Upload failed for {path}: {e}")
                retries += 1
                time.sleep(2 ** retries)
                continue
        except Exception as e:
            print(f"Upload error for {path}: {e}")
            retries += 1
            time.sleep(2 ** retries)
            continue
    print(f"Failed to upload {path} after {max_retries} retries.")
    return False

# ----------------------------------------------------------------------
# Worker – processes a fixed number of permutations per file
# ----------------------------------------------------------------------
def worker(start_idx, count, worker_id, run_id, stop_event, seed_words, total_perms):
    """
    Worker processes count permutation indexes, starting from start_idx.
    It chunks them into groups of PERMUTATIONS_PER_FILE, collects valid seeds,
    and uploads the resulting file(s). Progress is only advanced after upload.
    """
    dbx = get_dropbox_client()
    folder_path = DROPBOX_FOLDER_PATH.rstrip('/')
    words = seed_words[:]
    mnemo = Mnemonic("english")

    # We'll process ranges sequentially
    current = start_idx
    remaining = count
    pending = []  # (content, filename, path, chunk_size)

    while remaining > 0 and not stop_event.is_set():
        # Determine chunk size for this iteration
        chunk_size = min(remaining, PERMUTATIONS_PER_FILE)
        chunk_start = current
        chunk_end = chunk_start + chunk_size

        # Collect valid seeds in this chunk
        valid_seeds = []
        for idx in range(chunk_start, chunk_end):
            # Compute permutation for idx
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

        # If there are valid seeds, prepare a file for upload
        if valid_seeds:
            file_counter = chunk_start // PERMUTATIONS_PER_FILE + 1  # just a unique counter
            filename = f"seeds_{run_id}_w{worker_id}_{file_counter:08d}_{len(valid_seeds)}.txt"
            content = "\n".join(valid_seeds)
            path = f"{folder_path}/{filename}"
            pending.append((content, filename, path, chunk_size))
        else:
            # No seeds in this chunk – we can mark progress immediately
            update_progress(chunk_size, total_perms)
            print(f"Worker {worker_id} chunk [{chunk_start:,} - {chunk_end:,}] had no seeds, progress +{chunk_size:,}")

        # Move to next chunk
        current += chunk_size
        remaining -= chunk_size

        # If pending has reached the batch size, upload all and update progress
        if len(pending) >= UPLOAD_BATCH_SIZE:
            total_uploaded = 0
            total_increment = 0
            for content, fname, path, csize in pending:
                print(f"Worker {worker_id} uploading {fname}...")
                success = upload_with_retry(dbx, content, path)
                if success:
                    print(f"Worker {worker_id} uploaded {fname}")
                    total_uploaded += 1
                    total_increment += csize
                else:
                    # Should not happen because upload_with_retry retries indefinitely,
                    # but if it does, we block until success.
                    print(f"Worker {worker_id} FAILED to upload {fname} – retrying indefinitely...")
                    while not upload_with_retry(dbx, content, path, max_retries=100):
                        time.sleep(5)
                    total_uploaded += 1
                    total_increment += csize

            # Now update global progress for all uploaded files
            if total_increment > 0:
                update_progress(total_increment, total_perms)
                print(f"Worker {worker_id} progress +{total_increment:,} ({total_uploaded} files)")

            pending.clear()

    # After loop, handle any remaining pending files
    if pending and not stop_event.is_set():
        total_increment = 0
        for content, fname, path, csize in pending:
            print(f"Worker {worker_id} uploading final {fname}...")
            success = upload_with_retry(dbx, content, path)
            if success:
                print(f"Worker {worker_id} uploaded {fname}")
                total_increment += csize
            else:
                while not upload_with_retry(dbx, content, path, max_retries=100):
                    time.sleep(5)
                total_increment += csize
        if total_increment > 0:
            update_progress(total_increment, total_perms)
            print(f"Worker {worker_id} final progress +{total_increment:,}")

    print(f"Worker {worker_id} finished. Processed {count} indices. Stopped: {stop_event.is_set()}")

# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
def main():
    global stop_event

    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: Supabase credentials missing.")
        sys.exit(1)
    if not (DROPBOX_ACCESS_TOKEN or (DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET)):
        print("ERROR: Missing Dropbox credentials.")
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

    # Sanity check
    if progress > total_perms:
        print(f"WARNING: Progress ({progress}) exceeds total permutations ({total_perms}). Resetting to 0.")
        progress = 0
        set_progress(0)

    # Detect changed seed phrases
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
        return

    print(f"Total permutations: {total_perms:,}, already processed: {progress:,}, remaining: {remaining:,}")

    # Distribute remaining work among workers
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

    # Setup multiprocessing
    import multiprocessing as mp
    manager = mp.Manager()
    stop_event = manager.Event()

    def signal_handler(sig, frame):
        print("\nInterrupt received! Setting stop event...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)

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
        if final_progress >= total_perms:
            print("Generation completed!")
        else:
            print(f"Generation interrupted. Final progress: {final_progress:,} / {total_perms:,}")

if __name__ == "__main__":
    main()