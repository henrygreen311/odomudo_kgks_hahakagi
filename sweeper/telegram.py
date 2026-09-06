import aiohttp
import json
import tempfile
import os

async def send_telegram_message(token, chat_id, text, parse_mode="HTML"):
    """Send a plain text message to Telegram."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload, timeout=15) as resp:
                return resp.status == 200
    except Exception:
        return False

async def send_telegram_file(token, chat_id, content, caption=""):
    """Send a text file to Telegram."""
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        async with aiohttp.ClientSession() as session:
            with open(tmp_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("caption", caption)
                data.add_field("document", f, filename="sweep_log.txt")
                async with session.post(url, data=data, timeout=30) as resp:
                    ok = resp.status == 200
        os.unlink(tmp_path)
        return ok
    except Exception:
        return False

async def send_log(token, chat_id, text, max_len=4000):
    """Send either as message or file, depending on length."""
    if len(text) <= max_len:
        return await send_telegram_message(token, chat_id, text)
    else:
        return await send_telegram_file(token, chat_id, text, caption="Sweep log (too long for message)")
