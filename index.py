#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import argparse
from supabase import create_client

def load_db_config():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ):
        if os.path.exists(path):
            config = {}
            with open(path) as file:
                for line in file:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        config[key.strip()] = value.strip().strip('"')
            return config
    raise FileNotFoundError("db.txt not found")

def create_supabase_client():
    config = load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

def fetch_drive_credentials(supabase):
    response = supabase.table("brute").select("*").limit(1).execute()
    if not response.data:
        raise RuntimeError("No credentials found in 'brute' table.")
    row = response.data[0]
    creds = row.get("drive_credentials")
    token = row.get("drive_token")
    folder_id = row.get("drive_folder_id")
    return creds, token, folder_id

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true", help="Run only the generator")
    parser.add_argument("--scan", action="store_true", help="Run the scanner (generator will run first if needed)")
    args = parser.parse_args()

    config = load_db_config()
    os.environ["SUPABASE_URL"] = config["SUPABASE_URL"]
    os.environ["SUPABASE_KEY"] = config["SUPABASE_KEY"]

    supabase = create_supabase_client()
    creds_json, token_json, folder_id = fetch_drive_credentials(supabase)

    if creds_json:
        os.environ["DRIVE_CREDENTIALS"] = creds_json
    if token_json:
        os.environ["DRIVE_TOKEN"] = token_json
    if folder_id:
        os.environ["DRIVE_FOLDER_ID"] = folder_id

    run_generator = False
    run_scanner = False

    if args.generate:
        run_generator = True
    elif args.scan:
        run_generator = True
        run_scanner = True
    else:
        run_generator = True
        run_scanner = True

    if run_generator:
        print("Running generator...")
        gen_result = subprocess.run(["python3", "-m", "src.generator"])
        if gen_result.returncode != 0:
            print("Generator failed. Exiting.")
            sys.exit(gen_result.returncode)
        else:
            print("Generator completed successfully.")

    if run_scanner:
        print("Starting scanner (src.brute)...")
        subprocess.run(["python3", "-m", "src.brute"], check=True)

if __name__ == "__main__":
    main()