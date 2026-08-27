#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import math
import signal
import time
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
BATCH_SIZE = 10000
NUM_WORKERS = 10   # adjust as needed
PROGRESS_COLUMN = "generation_progress"

# Global stop event (shared via Manager)
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
    """Atomically increment progress with fallback."""
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

# ----------------------------------------------------------------------
# Worker function (runs in a separate process)
# ----------------------------------------------------------------------
def worker(start_idx, count, worker_id, run_id, stop_event):
    dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
    folder_path = DROPBOX_FOLDER_PATH.rstrip('/')

    try:
        with open("seed_phrases.txt", "r", encoding="utf-8") as f:
            words = f.read().strip().split()
        if len(words) != 12:
            raise ValueError("Need exactly 12 words")
    except Exception as e:
        print(f"Worker {worker_id} error loading seed words: {e}")
        return

    mnemo = Mnemonic("english")
    chunk = []
    file_counter = 0
    processed = 0
    i = 0

    for i in range(count):
        if stop_event.is_set():
            print(f"Worker {worker_id} received stop signal, finishing...")
            break

        current_idx = start_idx + i
        # Compute permutation inline
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

        # Upload if chunk full
        if len(chunk) >= BATCH_SIZE:
            file_counter += 1
            filename = f"seeds_{run_id}_w{worker_id}_{file_counter:08d}_{len(chunk)}.txt"
            content = "\n".join(chunk)
            path = f"{folder_path}/{filename}"
            try:
                dbx.files_upload(content.encode('utf-8'), path,
                                 mode=dropbox.files.WriteMode.overwrite,
                                 mute=True)
                print(f"Worker {worker_id} uploaded {filename}")
            except Exception as e:
                print(f"Worker {worker_id} upload error: {e}")
            chunk = []

        # Update progress every 10k iterations
        if (i + 1) % 10000 == 0:
            update_progress(10000)
            processed += 10000

    # Save remaining progress
    total_processed = i + 1 if i > 0 else 0
    remaining_progress = total_processed - processed
    if remaining_progress > 0:
        update_progress(remaining_progress)

    # Upload leftover chunk
    if chunk:
        file_counter += 1
        filename = f"seeds_{run_id}_w{worker_id}_{file_counter:08d}_{len(chunk)}.txt"
        content = "\n".join(chunk)
        path = f"{folder_path}/{filename}"
        try:
            dbx.files_upload(content.encode('utf-8'), path,
                             mode=dropbox.files.WriteMode.overwrite,
                             mute=True)
            print(f"Worker {worker_id} uploaded {filename}")
        except Exception as e:
            print(f"Worker {worker_id} upload error: {e}")

    print(f"Worker {worker_id} finished. Processed {total_processed} indices. Stopped early: {stop_event.is_set()}")

# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
def main():
    global stop_event

    if not all([SUPABASE_URL, SUPABASE_KEY, DROPBOX_ACCESS_TOKEN]):
        print("ERROR: Missing required environment variables.")
        sys.exit(1)

    try:
        with open("seed_phrases.txt", "r", encoding="utf-8") as f:
            words = f.read().strip().split()
        if len(words) != 12:
            print("ERROR: Need exactly 12 words in seed_phrases.txt")
            sys.exit(1)
        total_perms = math.factorial(12)
    except FileNotFoundError:
        print("ERROR: seed_phrases.txt not found.")
        sys.exit(1)

    progress = get_progress()
    if progress >= total_perms:
        print("All permutations already processed.")
        return

    remaining = total_perms - progress
    print(f"Total permutations: {total_perms:,}, already processed: {progress:,}, remaining: {remaining:,}")

    # Generate a unique run ID (timestamp)
    run_id = str(int(time.time()))

    # Split work
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
        futures = [executor.submit(worker, start, count, wid, run_id, stop_event)
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
        print(f"Final progress: {final_progress:,} / {total_perms:,}")

if __name__ == "__main__":
    main()