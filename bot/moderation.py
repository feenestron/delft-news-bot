"""Модерация черновика: кнопки «Опубликовать/Отклонить», правка ответом на черновик."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from .config import Config
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

HELP = (
    "Я присылаю сюда черновик утреннего дайджеста.\n\n"
    "• «✅ Опубликовать» — отправить в канал как есть.\n"
    "• «❌ Отклонить» — сегодня ничего не публиковать.\n"
    "• Чтобы поправить текст — ответьте (Reply) на черновик исправленной версией: "
    "я пришлю новый черновик с кнопками.\n\n"
    "Команды: /status — что с черновиком, /publish, /reject.\n"
    "Нажатия обрабатываются утром сразу, а после "
    "окончания утреннего окна — раз в час."
)


def keyboard(draft_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"pub:{draft_id}"},
        {"text": "❌ Отклонить", "callback_data": f"rej:{draft_id}"},
    ]]}


def post_link(channel_id: str, message_id: int) -> str:
    if channel_id.startswith("@"):
        return f"https://t.me/{channel_id[1:]}/{message_id}"
    if channel_id.startswith("-100"):
        return f"https://t.me/c/{channel_id[4:]}/{message_id}"
    return f"сообщение #{message_id}"


def publish_hint(description: str) -> str:
    d = description.lower()
    if "can't send messages to the bot" in d:
        return ("\n\nВ секрете CHANNEL_ID указан бот, а не канал. Укажите @имя публичного канала "
                "или числовой id вида -100… (бот присылает его, когда его добавляют в канал).")
    if "chat not found" in d:
        return ("\n\nКанал не найден: проверьте CHANNEL_ID (@имя канала или -100…) и что бот "
                "добавлен в канал.")
    if "not enough rights" in d or "need administrator" in d or "not a member" in d:
        return "\n\nСделайте бота администратором канала с правом публикации сообщений."
    return ""


class Moderator:
    def __init__(self, cfg: Config, tg: Telegram, state: dict, save: Callable[[], None],
                 now: Callable[[], datetime]):
        self.cfg, self.tg, self.state, self.save, self.now = cfg, tg, state, save, now

    @property
    def pending(self) -> dict | None:
        return self.state.get("pending")

    def is_open(self) -> bool:
        p = self.pending
        return bool(p and p.get("status") == "pending")

    # -------------------------------------------------------------- draft
    def submit_draft(self, draft_id: str, post: dict, service_note: str, event_urls: list[str],
                     news_urls: list[str] | None = None) -> None:
        self.supersede()
        admin = self.cfg.admin_chat_id
        self.tg.send_text(admin, service_note)
        msg = self.tg.send(admin, post, reply_markup=keyboard(draft_id))
        self.state["pending"] = {
            "id": draft_id, "status": "pending", "post": post,
            "preview_ids": [msg["message_id"]], "event_urls": event_urls, "news_urls": news_urls or [],
            "created": self.now().isoformat(), "note": service_note,
        }
        self.save()

    def supersede(self) -> None:
        p = self.pending
        if p and p.get("status") == "pending":
            for mid in p.get("preview_ids", []):
                self.tg.remove_buttons(self.cfg.admin_chat_id, mid)
            p["status"] = "superseded"
            self._archive(p)

    def _archive(self, p: dict) -> None:
        self.state["history"].append({k: p.get(k) for k in
                                      ("id", "status", "created", "decided", "channel_message_id")})

    # -------------------------------------------------------------- actions
    def publish(self) -> str:
        p = self.pending
        if not p or p["status"] != "pending":
            return "Нет черновика, ожидающего решения."
        p["status"] = "publishing"   # защита от двойной публикации при сбое
        self.save()
        try:
            msg = self.tg.send(self.cfg.channel_id, p["post"])
        except TelegramError as exc:
            p["status"] = "pending"
            p["last_error"] = f"{self.now().isoformat()} {exc.description}"
            self.save()
            return f"⚠️ Не удалось опубликовать: {exc.description}" + publish_hint(exc.description)
        p.update(status="published", decided=self.now().isoformat(),
                 channel_message_id=msg["message_id"])
        stamp = self.now().isoformat()
        for url in p.get("event_urls", []):
            self.state["announced_events"][url] = stamp
        for url in p.get("news_urls", []):
            self.state["seen"][url] = stamp
        for mid in p.get("preview_ids", []):
            self.tg.remove_buttons(self.cfg.admin_chat_id, mid)
        self._archive(p)
        self.save()
        return f"✅ Опубликовано: {post_link(self.cfg.channel_id, msg['message_id'])}"

    def reject(self) -> str:
        p = self.pending
        if not p or p["status"] != "pending":
            return "Нет черновика, ожидающего решения."
        p.update(status="rejected", decided=self.now().isoformat())
        for mid in p.get("preview_ids", []):
            self.tg.remove_buttons(self.cfg.admin_chat_id, mid)
        self._archive(p)
        self.save()
        return "❌ Черновик отклонён, сегодня ничего не публикуется."

    def status(self) -> str:
        p = self.pending
        if not p:
            return "Черновиков пока не было."
        names = {"pending": "ждёт решения", "publishing": "публикуется (проверьте канал)",
                 "published": "опубликован", "rejected": "отклонён", "superseded": "заменён новым"}
        return f"Черновик {p['id']}: {names.get(p['status'], p['status'])}."

    def replace_text(self, message: dict) -> str | None:
        text = message.get("text")
        if not text:
            return "Пришлите исправленный дайджест текстом (ответом на черновик)."
        p = self.pending
        p["post"] = {"text": text, "entities": message.get("entities") or []}
        for mid in p["preview_ids"]:
            self.tg.remove_buttons(self.cfg.admin_chat_id, mid)
        try:
            msg = self.tg.send(self.cfg.admin_chat_id, p["post"], reply_markup=keyboard(p["id"]))
        except TelegramError as exc:
            return f"⚠️ Не удалось подготовить новый черновик: {exc.description}"
        p["preview_ids"].append(msg["message_id"])
        self.save()
        return None  # новый черновик сам по себе является ответом

    # -------------------------------------------------------------- updates
    def handle(self, update: dict) -> None:
        admin = self.cfg.admin_chat_id
        if "callback_query" in update:
            cq = update["callback_query"]
            if admin is None or cq.get("from", {}).get("id") != admin:
                self.tg.answer_callback(cq["id"], "Нет доступа")
                return
            action, _, draft_id = (cq.get("data") or "").partition(":")
            p = self.pending
            if not p or p["id"] != draft_id:
                self.tg.answer_callback(cq["id"], "Этот черновик устарел")
                return
            reply = self.publish() if action == "pub" else self.reject() if action == "rej" else None
            self.tg.answer_callback(cq["id"], "Готово" if reply else "")
            if reply:
                self.tg.send_text(admin, reply)
            return

        if "my_chat_member" in update:
            self._on_added(update["my_chat_member"])
            return

        message = update.get("message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        if admin is None:
            # Бот ещё не настроен: подсказываем chat_id, чтобы записать его в секрет ADMIN_CHAT_ID.
            log.info("Сообщение от chat_id=%s (%s)", chat_id, message["chat"].get("username"))
            self.tg.send_text(chat_id, f"Ваш chat_id: {chat_id}\nУкажите его в секрете ADMIN_CHAT_ID.")
            return
        if chat_id != admin:
            return
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        if command in ("/start", "/help"):
            answer = HELP
        elif command == "/status":
            answer = self.status()
        elif command == "/publish":
            answer = self.publish()
        elif command == "/reject":
            answer = self.reject()
        elif self.is_open() and reply_to in self.pending.get("preview_ids", []):
            answer = self.replace_text(message)
        else:
            answer = "Чтобы поправить черновик, ответьте (Reply) именно на него. /help — подсказка."
        if answer:
            self.tg.send_text(admin, answer)

    def _on_added(self, upd: dict) -> None:
        """Бота добавили в канал/группу (или убрали) — сообщаем модератору id этого чата."""
        admin = self.cfg.admin_chat_id
        chat = upd.get("chat", {})
        status = (upd.get("new_chat_member") or {}).get("status")
        if admin is None or chat.get("type") == "private":
            return
        kind = {"channel": "канал", "group": "группу", "supergroup": "группу"}.get(chat.get("type"), "чат")
        title = chat.get("title", "")
        if status in ("administrator", "member"):
            ident = f"@{chat['username']}" if chat.get("username") else str(chat.get("id"))
            text = (f"Меня добавили в {kind} «{title}» ({'администратором' if status == 'administrator' else 'участником'}).\n"
                    f"Чтобы публиковать туда, укажите в секрете CHANNEL_ID: {ident}\n"
                    f"(числовой id: {chat.get('id')})")
            if status != "administrator":
                text += "\n\n⚠️ Для публикации сделайте меня администратором с правом отправки сообщений."
        else:
            text = f"Меня убрали из «{title}» (статус: {status})."
        self.tg.send_text(admin, text)

    def poll(self, until: datetime | None = None) -> None:
        """Обрабатывает входящие обновления. С until — ждёт, пока черновик не решён или не наступит until."""
        while True:
            remaining = int((until - self.now()).total_seconds()) if until else 0
            long_poll = max(0, min(50, remaining)) if until else 0
            updates = self.tg.get_updates(self.state.get("tg_offset", 0), long_poll)
            for u in updates:
                self.processed = getattr(self, "processed", 0) + 1
                self.state["tg_offset"] = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception:
                    log.exception("Ошибка обработки обновления %s", u.get("update_id"))
                self.save()
            if updates:
                continue  # могли прийти ещё
            if until is None or not self.is_open() or self.now() >= until:
                return
