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

def fetch_dropbox_credentials(supabase):
    response = supabase.table("brute").select("*").limit(1).execute()
    if not response.data:
        raise RuntimeError("No credentials found in 'brute' table.")
    row = response.data[0]
    token = row.get("dropbox_access_token")
    folder_path = row.get("dropbox_folder_path")
    if not token:
        raise ValueError("dropbox_access_token column is empty.")
    if not folder_path:
        folder_path = "/"
    return token, folder_path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()

    if not args.generate and not args.scan:
        print("Please specify --generate or --scan")
        sys.exit(1)

    # Load db.txt and set all env vars needed by generator.py
    config = load_db_config()
    os.environ["SUPABASE_URL"] = config["SUPABASE_URL"]
    os.environ["SUPABASE_KEY"] = config["SUPABASE_KEY"]

    # Get Dropbox credentials from the brute table
    supabase = create_supabase_client()
    token, folder_path = fetch_dropbox_credentials(supabase)
    os.environ["DROPBOX_ACCESS_TOKEN"] = token
    os.environ["DROPBOX_FOLDER_PATH"] = folder_path

    if args.generate:
        # Run src.generator as a module
        subprocess.run(["python3", "-m", "src.generator"], check=True)
    elif args.scan:
        subprocess.run(["python3", "brute.py", "--scan"], check=True)

if __name__ == "__main__":
    main()