"""Минимальная обёртка над Telegram Bot API."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, description: str, code: int | None = None):
        super().__init__(f"Telegram API {code}: {description}")
        self.code = code
        self.description = description


class Telegram:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = session or requests.Session()

    def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        timeout = payload.get("timeout", 0) + 30
        for attempt in range(5):
            try:
                r = self.session.post(self.base + method, json=payload, timeout=timeout)
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 4:
                    raise
                log.warning("Telegram %s: %s, повтор", method, exc)
                time.sleep(2 + attempt * 3)
                continue
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry and attempt < 4:
                time.sleep(int(retry) + 1)
                continue
            raise TelegramError(data.get("description", "unknown error"), data.get("error_code"))
        raise TelegramError("слишком много повторов")

    # ---- удобные методы
    def send(self, chat_id, post: dict, reply_markup: dict | None = None,
             reply_to: int | None = None) -> dict:
        """post = {"text": ..., "parse_mode": "HTML"} или {"text": ..., "entities": [...]}"""
        return self.call(
            "sendMessage", chat_id=chat_id, text=post["text"],
            parse_mode=post.get("parse_mode"), entities=post.get("entities"),
            link_preview_options={"is_disabled": True}, reply_markup=reply_markup,
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
            if reply_to else None,
        )

    def send_text(self, chat_id, text: str, reply_to: int | None = None) -> dict:
        return self.send(chat_id, {"text": text}, reply_to=reply_to)

    def remove_buttons(self, chat_id, message_id: int) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                      reply_markup={"inline_keyboard": []})
        except TelegramError as exc:  # сообщение могло быть удалено или уже без кнопок
            log.info("Не удалось убрать кнопки: %s", exc)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)
        except TelegramError as exc:  # устаревший callback — не страшно
            log.info("answerCallbackQuery: %s", exc)

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query", "my_chat_member"])
