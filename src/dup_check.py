#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import time
from collections import defaultdict
from supabase import create_client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ------------------ DB CONFIG ------------------
def load_db_config():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
        os.path.join(os.path.dirname(os.path.dirname(here)), "db.txt"),
    ):
        if os.path.exists(path):
            config = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        config[key.strip()] = value.strip().strip('"')
            return config
    raise FileNotFoundError("db.txt not found")

def get_supabase_client():
    cfg = load_db_config()
    return create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])

def fetch_drive_credentials(supabase):
    res = supabase.table("brute").select("*").limit(1).execute()
    if not res.data:
        raise RuntimeError("No row in 'brute' table.")
    row = res.data[0]
    return row.get("drive_credentials"), row.get("drive_token"), row.get("drive_folder_id")

# ------------------ GOOGLE DRIVE ------------------
def get_drive_service(creds_json, token_json):
    if not creds_json or not token_json:
        raise RuntimeError("Missing Google Drive credentials in brute table")
    token_info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(
        info=token_info,
        scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive", "v3", credentials=creds)

def list_all_files(service, folder_id):
    files = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and name contains 'seeds_' and trashed=false",
            fields="nextPageToken, files(id, name, md5Checksum, size, createdTime)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return files

def delete_file(service, file_id, file_name, max_retries=10):
    retries = 0
    while True:
        try:
            service.files().delete(fileId=file_id).execute()
            return True
        except Exception as e:
            retries += 1
            if retries > max_retries:
                print(f"  FAILED to delete {file_name} after {max_retries} retries: {e}")
                return False
            wait = min(2 ** retries, 30)
            time.sleep(wait)

# ------------------ FILENAME PARSING ------------------
# Filename format: seeds_<run_id>_w<worker_id>_<counter>_<seed_count>.txt
FILENAME_PATTERN = re.compile(r"^seeds_(\d+)_w(\d+)_(\d+)_(\d+)\.txt$")

def parse_seed_count(filename):
    m = FILENAME_PATTERN.match(filename)
    if m:
        return int(m.group(4))
    return None

# ------------------ MAIN ------------------
def main():
    print("Starting duplicate + size check...")

    supabase = get_supabase_client()
    creds_json, token_json, folder_id = fetch_drive_credentials(supabase)

    if not folder_id:
        raise RuntimeError("drive_folder_id is empty in brute table")

    service = get_drive_service(creds_json, token_json)

    files = list_all_files(service, folder_id)
    total_files = len(files)
    print(f"Found {total_files} files in Drive.")

    # ---------- PART 1: count small files ----------
    print("\n=== Files with fewer than 50,000 seeds ===")
    small_files = []
    unparsed = []
    for f in files:
        count = parse_seed_count(f["name"])
        if count is None:
            unparsed.append(f["name"])
            continue
        if count < 50000:
            small_files.append((f["name"], count))

    print(f"Small files: {len(small_files)}")

    if unparsed:
        print(f"Unparsed filenames: {len(unparsed)}")

    # ---------- PART 2: duplicate MD5 check ----------
    print("\n=== Duplicate content check (MD5) ===")
    by_hash = defaultdict(list)
    missing_hash = 0
    for f in files:
        md5 = f.get("md5Checksum")
        if not md5:
            missing_hash += 1
            continue
        by_hash[md5].append(f)

    if missing_hash:
        print(f"Files with missing MD5: {missing_hash}")

    duplicate_groups = {h: flist for h, flist in by_hash.items() if len(flist) > 1}

    if not duplicate_groups:
        print("No duplicates found. Everything is clean.")
        return

    total_duplicates = sum(len(flist) - 1 for flist in duplicate_groups.values())
    print(f"Duplicate groups: {len(duplicate_groups)}  |  Redundant files: {total_duplicates}")

    deleted_count = 0
    for md5, flist in duplicate_groups.items():
        flist.sort(key=lambda x: x.get("createdTime", ""))
        keep = flist[0]
        to_delete = flist[1:]

        for f in to_delete:
            if delete_file(service, f["id"], f["name"]):
                deleted_count += 1

    print(f"\nDone. Deleted {deleted_count} duplicate files out of {total_files} total.")

if __name__ == "__main__":
    main()