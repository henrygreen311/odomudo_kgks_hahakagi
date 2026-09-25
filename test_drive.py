#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from supabase import create_client

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
TEST_FILE = "test.txt"


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


def get_supabase():
    cfg = load_db_config()
    return create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])


# ------------------ TOKEN HELPERS ------------------
def _token_json_from_env_or_file():
    token_env = os.getenv("DRIVE_TOKEN")
    if token_env:
        return token_env
    if Path(TOKEN_FILE).exists():
        return Path(TOKEN_FILE).read_text()
    return None


def authenticate():
    creds = None

    token_json = _token_json_from_env_or_file()
    if token_json:
        try:
            token_info = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        except Exception as e:
            print(f"Failed to load token JSON: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Access token expired – refreshing with refresh_token...")
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise RuntimeError(
                    "No valid credentials and credentials.json missing. "
                    "Run `python3 test_drive.py` interactively once to generate token.json."
                )
            print("No valid token – starting interactive OAuth flow...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

    Path(TOKEN_FILE).write_text(creds.to_json())
    return creds


def update_token_in_db(creds):
    """Push fresh token JSON to the brute table's drive_token column."""
    try:
        supabase = get_supabase()
        row = supabase.table("brute").select("id").limit(1).execute()
        if not row.data:
            print("WARNING: No row in brute table; cannot update token.")
            return False
        row_id = row.data[0]["id"]
        supabase.table("brute").update({"drive_token": creds.to_json()}).eq("id", row_id).execute()
        print("Updated drive_token in DB.")
        return True
    except Exception as e:
        print(f"Failed to update token in DB: {e}")
        return False


def refresh_and_update_token():
    """
    Refresh token, save to disk, push to DB, and update env vars for this process.
    Returns the credentials object.
    """
    creds = authenticate()
    update_token_in_db(creds)

    # Update env vars so subsequent code can use them
    os.environ["DRIVE_TOKEN"] = creds.to_json()

    # Also reload DRIVE_CREDENTIALS / DRIVE_FOLDER_ID from DB in case they changed
    try:
        supabase = get_supabase()
        row = supabase.table("brute").select("drive_credentials, drive_folder_id").limit(1).execute()
        if row.data:
            creds_json = row.data[0].get("drive_credentials")
            folder_id = row.data[0].get("drive_folder_id")
            if creds_json:
                os.environ["DRIVE_CREDENTIALS"] = creds_json
            if folder_id:
                os.environ["DRIVE_FOLDER_ID"] = folder_id
    except Exception:
        pass

    return creds


# ------------------ TEST UPLOAD (CLI only) ------------------
def do_test_upload():
    creds = refresh_and_update_token()
    service = build("drive", "v3", credentials=creds)

    metadata = {"name": Path(TEST_FILE).name}
    media = MediaFileUpload(TEST_FILE, mimetype="text/plain", resumable=True)
    result = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,size"
    ).execute()

    print("Upload successful!")
    print("Name:", result["name"])
    print("ID:", result["id"])
    print("Size:", result["size"])


if __name__ == "__main__":
    do_test_upload()