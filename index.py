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
    folder_path = row.get("dropbox_folder_path", "/")
    app_key = row.get("dropbox_app_key")
    app_secret = row.get("dropbox_app_secret")
    refresh_token = row.get("dropbox_refresh_token")
    return token, folder_path, app_key, app_secret, refresh_token

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()

    if not args.generate and not args.scan:
        print("Please specify --generate or --scan")
        sys.exit(1)

    config = load_db_config()
    os.environ["SUPABASE_URL"] = config["SUPABASE_URL"]
    os.environ["SUPABASE_KEY"] = config["SUPABASE_KEY"]

    supabase = create_supabase_client()
    token, folder_path, app_key, app_secret, refresh_token = fetch_dropbox_credentials(supabase)

    if token:
        os.environ["DROPBOX_ACCESS_TOKEN"] = token
    if app_key:
        os.environ["DROPBOX_APP_KEY"] = app_key
    if app_secret:
        os.environ["DROPBOX_APP_SECRET"] = app_secret
    if refresh_token:
        os.environ["DROPBOX_REFRESH_TOKEN"] = refresh_token
    os.environ["DROPBOX_FOLDER_PATH"] = folder_path

    if args.generate:
        subprocess.run(["python3", "-m", "src.generator"], check=True)
    elif args.scan:
        print("Starting Dropbox scanner (src.brute)...")
        subprocess.run(["python3", "-m", "src.brute"], check=True)

if __name__ == "__main__":
    main()