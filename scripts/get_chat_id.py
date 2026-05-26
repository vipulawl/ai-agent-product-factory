"""Run this after starting a chat with your Telegram bot to find your TELEGRAM_CHAT_ID."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.environ["TELEGRAM_BOT_TOKEN"]
resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
updates = resp.json().get("result", [])
if not updates:
    print("No messages found. Send any message to your bot first, then run this again.")
else:
    for u in updates:
        if "message" in u:
            chat = u["message"]["chat"]
            print(f"TELEGRAM_CHAT_ID={chat['id']}  (name: {chat.get('first_name', '')} {chat.get('last_name', '')})")
