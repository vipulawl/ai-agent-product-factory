import os
import json
import logging
import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_openai() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


def chat(messages: list, model: str = "gpt-4o", json_mode: bool = False, temperature: float = 0.3) -> str:
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = get_openai().chat.completions.create(**kwargs)
    return resp.choices[0].message.content


def chat_json(messages: list, model: str = "gpt-4o") -> dict:
    raw = chat(messages, model=model, json_mode=True)
    return json.loads(raw)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/sendMessage",
            json={"chat_id": _tg_chat_id(), "text": text, "parse_mode": parse_mode},
            timeout=10
        )
        return resp.ok
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def poll_telegram_updates(offset: int = 0) -> tuple[list, int]:
    """Returns (list of messages, next_offset)."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{_tg_token()}/getUpdates",
            params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
            timeout=40
        )
        if not resp.ok:
            return [], offset
        updates = resp.json().get("result", [])
        if not updates:
            return [], offset
        next_offset = updates[-1]["update_id"] + 1
        messages = [
            u["message"]["text"]
            for u in updates
            if "message" in u and "text" in u["message"]
            and str(u["message"]["chat"]["id"]) == _tg_chat_id()
        ]
        return messages, next_offset
    except Exception as e:
        log.error(f"Telegram poll failed: {e}")
        return [], offset
