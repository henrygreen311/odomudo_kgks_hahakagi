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
# Environment
# ----------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_FOLDER_PATH = os.getenv("DROPBOX_FOLDER_PATH", "/")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
BATCH_SIZE = 100000
NUM_WORKERS = 20
PROGRESS_COLUMN = "generation_progress"
PREVIOUS_SEED_COLUMN = "previous_seed_phrases"
UPLOAD_BATCH_SIZE = 5

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

def update_progress(increment):
    supabase = get_supabase()
    row_id = get_row_id()
    try:
        supabase.rpc("increment_progress", {"inc": increment}).execute()
    except Exception:
        try:
            current = supabase.table("brute").select(PROGRESS_COLUMN).eq("id", row_id).execute()
            if current.data:
                new_value = current.data[0][PROGRESS_COLUMN] + increment
                supabase.table("brute").update({PROGRESS_COLUMN: new_value}).eq("id", row_id).execute()
        except Exception as e:
            print(f"Progress update failed: {e}")

def set_progress(value):
    """Set progress to a specific value."""
    supabase = get_supabase()
    row_id = get_row_id()
    try:
        supabase.table("brute").update({PROGRESS_COLUMN: value}).eq("id", row_id).execute()
    except Exception as e:
        print(f"Failed to set progress: {e}")

def get_seed_phrases():
    """Fetch the seed_phrases column from the brute table."""
    supabase = get_supabase()
    res = supabase.table("brute").select("seed_phrases").limit(1).execute()
    if not res.data:
        raise RuntimeError("No row found in 'brute' table.")
    seed_phrases = res.data[0].get("seed_phrases")
    if not seed_phrases:
        # Fallback: read from local file if column is empty
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
    """Fetch the previous_seed_phrases column."""
    supabase = get_supabase()
    res = supabase.table("brute").select(PREVIOUS_SEED_COLUMN).limit(1).execute()
    if not res.data:
        raise RuntimeError("No row found in 'brute' table.")
    return res.data[0].get(PREVIOUS_SEED_COLUMN)

def set_previous_seed_phrases(seed_str):
    """Update previous_seed_phrases column."""
    supabase = get_supabase()
    row_id = get_row_id()
    try:
        supabase.table("brute").update({PREVIOUS_SEED_COLUMN: seed_str}).eq("id", row_id).execute()
    except Exception as e:
        print(f"Failed to set previous seed phrases: {e}")

# ----------------------------------------------------------------------
# Dropbox client factory (auto‑refresh via refresh token)
# ----------------------------------------------------------------------
def get_dropbox_client():
    """Return a Dropbox client that auto‑refreshes if refresh credentials exist."""
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
# Upload helper with retry
# ----------------------------------------------------------------------
def upload_with_retry(dbx, content, path, max_retries=10):
    """Upload a file with exponential backoff; returns True on success."""
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
# Worker function – accumulates up to 10 chunks then uploads
# ----------------------------------------------------------------------
def worker(start_idx, count, worker_id, run_id, stop_event, seed_words):
    dbx = get_dropbox_client()
    folder_path = DROPBOX_FOLDER_PATH.rstrip('/')
    words = seed_words[:]
    mnemo = Mnemonic("english")

    chunk = []
    pending = []
    file_counter = 0
    processed = 0
    i = 0

    for i in range(count):
        if stop_event.is_set():
            print(f"Worker {worker_id} received stop signal, finishing...")
            break

        current_idx = start_idx + i
        arr = words[:]
        k = current_idx
        perm = []
        for j in range(12, 0, -1):
            fact = math.factorial(j - 1)
            idx = k // fact
            k %= fact
            perm.append(arr.pop(idx))
        mnemonic = ' '.join(perm)

        if mnemo.check(mnemonic):
            chunk.append(mnemonic)

        if len(chunk) >= BATCH_SIZE:
            file_counter += 1
            filename = f"seeds_{run_id}_w{worker_id}_{file_counter:08d}_{len(chunk)}.txt"
            content = "\n".join(chunk)
            path = f"{folder_path}/{filename}"
            pending.append((content, filename, path))
            chunk = []

            if len(pending) >= UPLOAD_BATCH_SIZE:
                for content, fname, path in pending:
                    print(f"Worker {worker_id} uploading {fname}...")
                    success = upload_with_retry(dbx, content, path)
                    if success:
                        print(f"Worker {worker_id} uploaded {fname}")
                    else:
                        print(f"Worker {worker_id} FAILED to upload {fname} – pausing and retrying indefinitely...")
                        while not upload_with_retry(dbx, content, path, max_retries=100):
                            print(f"Worker {worker_id} retrying {fname}...")
                            time.sleep(5)
                pending.clear()

        if (i + 1) % 10000 == 0:
            update_progress(10000)
            processed += 10000

    if chunk:
        file_counter += 1
        filename = f"seeds_{run_id}_w{worker_id}_{file_counter:08d}_{len(chunk)}.txt"
        content = "\n".join(chunk)
        path = f"{folder_path}/{filename}"
        pending.append((content, filename, path))
        chunk = []

    if pending:
        for content, fname, path in pending:
            print(f"Worker {worker_id} uploading {fname}...")
            success = upload_with_retry(dbx, content, path)
            if success:
                print(f"Worker {worker_id} uploaded {fname}")
            else:
                print(f"Worker {worker_id} FAILED to upload {fname} – pausing and retrying indefinitely...")
                while not upload_with_retry(dbx, content, path, max_retries=100):
                    print(f"Worker {worker_id} retrying {fname}...")
                    time.sleep(5)

    total_processed = i + 1 if i > 0 else 0
    remaining_progress = total_processed - processed
    if remaining_progress > 0:
        update_progress(remaining_progress)

    print(f"Worker {worker_id} finished. Processed {total_processed} indices. Stopped early: {stop_event.is_set()}")

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    global stop_event

    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: Supabase credentials missing.")
        sys.exit(1)
    if not (DROPBOX_ACCESS_TOKEN or (DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET)):
        print("ERROR: Missing Dropbox credentials (access token or refresh token + app credentials).")
        sys.exit(1)

    # Fetch current seed phrases and previous completed ones
    try:
        seed_words = get_seed_phrases()
        current_seed_str = ' '.join(seed_words)
        print(f"Current seed phrases: {current_seed_str}")
    except Exception as e:
        print(f"ERROR: Could not load seed phrases: {e}")
        sys.exit(1)

    total_perms = math.factorial(len(seed_words))

    # Get previous seed phrases and progress
    previous_seed_str = get_previous_seed_phrases()
    progress = get_progress()

    # Check if the current seed phrases have been completed before
    if previous_seed_str:
        if current_seed_str == previous_seed_str:
            # Same seed phrases as previous run
            if progress >= total_perms:
                print("\n" + "="*60)
                print("This seed phrases has been completed.")
                print("Update a new seed phrases when brute.py has done scanning all the valid seed phrases.")
                print("="*60 + "\n")
                sys.exit(0)  # Exit cleanly, no need to generate
            else:
                # Resume from where we left off
                print(f"Resuming from progress: {progress:,} / {total_perms:,}")
        else:
            # New seed phrases – reset progress and update previous
            print(f"New seed phrases detected. Resetting progress.")
            progress = 0
            set_progress(0)
            set_previous_seed_phrases(current_seed_str)
    else:
        # No previous seed phrases stored – first run
        print("First run detected. Setting previous seed phrases and starting from 0.")
        progress = 0
        set_progress(0)
        set_previous_seed_phrases(current_seed_str)

    remaining = total_perms - progress
    if remaining <= 0:
        print("All permutations already processed.")
        return

    print(f"Total permutations: {total_perms:,}, already processed: {progress:,}, remaining: {remaining:,}")

    run_id = str(int(time.time()))

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

    import multiprocessing as mp
    manager = mp.Manager()
    stop_event = manager.Event()

    def signal_handler(sig, frame):
        print("\nInterrupt received! Setting stop event...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)

    with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = [executor.submit(worker, start, count, wid, run_id, stop_event, seed_words)
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
            # Mark as completed by updating previous_seed_phrases (already set)
            print("Generation completed!")
        else:
            print(f"Generation interrupted. Final progress: {final_progress:,} / {total_perms:,}")

if __name__ == "__main__":
    main()