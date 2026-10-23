"""Точка входа.

    python -m bot digest [--force] [--dry-run [--ignore-seen]]
        Собрать новости и анонсы и сразу опубликовать дайджест в группу/канал (CHANNEL_ID).
        Без --force публикует один раз в день, с POST_HOUR до POST_UNTIL_HOUR.
        --dry-run печатает дайджест и кандидатов, ничего не отправляет и не меняет состояние.

    python -m bot chats
        Показать группы и каналы, куда добавлен бот, и их chat_id (для секрета CHANNEL_ID).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

from . import state as state_mod
from .collect import collect, load_sources
from .config import Config
from .digest import digest_to_dict, llm_digest, plain_digest, render
from .fetch import Fetcher
from .telegram import Telegram, TelegramError

log = logging.getLogger("bot")


def write_summary(text: str) -> None:
    log.info("\n%s", text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")


ARCHIVE_DAYS = 7


def save_archive(state_path: str, digest, now: datetime) -> None:
    """Сохраняет опубликованный дайджест в JSON рядом с состоянием:
    state/latest.json и state/digests/ГГГГ-ММ-ДД.json (последние ARCHIVE_DAYS выпусков)."""
    base = os.path.dirname(state_path) or "."
    folder = os.path.join(base, "digests")
    os.makedirs(folder, exist_ok=True)
    data = json.dumps(digest_to_dict(digest, now), ensure_ascii=False, indent=1)
    for path in (os.path.join(base, "latest.json"), os.path.join(folder, f"{digest.day.isoformat()}.json")):
        with open(path, "w", encoding="utf-8") as f:
            f.write(data + "\n")
    days = sorted(n[:-5] for n in os.listdir(folder) if n.endswith(".json") and n != "index.json")
    for day in days[:-ARCHIVE_DAYS]:
        os.remove(os.path.join(folder, day + ".json"))
    with open(os.path.join(folder, "index.json"), "w", encoding="utf-8") as f:
        json.dump(sorted(days[-ARCHIVE_DAYS:], reverse=True), f)


def due(cfg: Config, st: dict, now: datetime) -> tuple[bool, str]:
    if st.get("last_post_date") == now.date().isoformat():
        return False, "сегодня дайджест уже был"
    # Расписание GitHub может опаздывать на часы, поэтому публикуем при первом запуске в окне.
    if not (cfg.post_hour <= now.hour < max(cfg.post_until_hour, cfg.post_hour + 1)):
        return False, f"сейчас {now:%H:%M}, публикация с {cfg.post_hour}:00 до {cfg.post_until_hour}:00"
    return True, ""


def report(collected, text: str, problems: list[str], now: datetime, digest=None) -> str:
    """Отчёт запуска: источники, проблемы, сам пост и все кандидаты."""
    lines = [f"# Дайджест {now:%Y-%m-%d %H:%M}", "", "## Источники", ""]
    lines += [f"- {k}: {v}" for k, v in collected.per_source.items()]
    if problems:
        lines += ["", "## Проблемы", ""] + [f"- {p}" for p in problems]
    lines += ["", "## Пост (HTML для Telegram)", ""]
    lines += ["```html", text, "```"] if text else ["_Публиковать нечего._"]
    if digest is not None and digest.skipped:
        lines += ["", f"## Не вошло в выпуск ({len(digest.skipped)})", ""]
        for s in digest.skipped:
            flag = " ⚠️ ключевое слово" if s.get("flagged") else ""
            lines.append(f"- [{s['source']}] [{s['original']}]({s['url']}) — {s['reason']}{flag}")
    lines += ["", f"## Кандидаты: новости ({len(collected.news)})", ""]
    for n in collected.news:
        when = n.published.strftime("%d.%m %H:%M") if n.published else "без даты"
        lines.append(f"- [{n.source}] {when} — [{n.title}]({n.url})")
    lines += ["", f"## Кандидаты: события ({len(collected.events)})", ""]
    for e in collected.events:
        dates = e.start.isoformat() if e.start else "?"
        if e.end and e.end != e.start:
            dates += f"..{e.end.isoformat()}"
        extra = " · ".join(x for x in (e.time_text, e.venue, e.price) if x)
        lines.append(f"- [{e.source}] {dates} — [{e.title}]({e.url})" + (f" ({extra})" if extra else ""))
    return "\n".join(lines) + "\n"


def publish(tg: Telegram, chat_id: str, messages: list[str], problems: list[str]) -> int:
    """Отправляет сообщения дайджеста по порядку; возвращает id первого."""
    first = None
    for text in messages:
        post = {"text": text, "parse_mode": "HTML"}
        try:
            sent = tg.send(chat_id, post)
        except TelegramError as exc:
            new_id = exc.parameters.get("migrate_to_chat_id")
            if not new_id:
                raise
            # группа стала супергруппой — у неё новый id
            problems.append(f"группа сменила id на {new_id} — обновите секрет CHANNEL_ID")
            chat_id = str(new_id)
            sent = tg.send(chat_id, post)
        first = first or sent["message_id"]
    return first


def cmd_digest(cfg: Config, args) -> int:
    now = datetime.now(cfg.tz)
    st = state_mod.load(cfg.state_path)
    if not args.force and not args.dry_run:
        ok, why = due(cfg, st, now)
        if not ok:
            log.info("Пропускаю: %s", why)
            return 0
    if not args.dry_run:
        cfg.require_telegram()

    window = cfg.news_window_hours
    collect_state = st
    if args.dry_run and getattr(args, "ignore_seen", False):
        # Для настройки отбора: берём все свежие материалы за 3 дня, даже уже опубликованные.
        import copy
        collect_state = copy.deepcopy(st)
        collect_state["seen"], collect_state["announced_events"] = {}, {}
        window = max(window, 72)
    collected = collect(Fetcher(), collect_state, now, load_sources(cfg.sources_path),
                        window, cfg.event_horizon_days)
    log.info("Собрано: новостей %d, событий %d, ошибок %d",
             len(collected.news), len(collected.events), len(collected.errors))
    problems = list(collected.errors)
    horizon_end = now.date() + timedelta(days=cfg.event_horizon_days)

    digest = None
    if collected.news or collected.events:
        if cfg.anthropic_key:
            try:
                digest = llm_digest(collected, now, horizon_end, cfg.anthropic_key, cfg.model,
                                    cfg.max_news, cfg.max_events)
            except Exception as exc:
                log.exception("Claude не сработал")
                problems.append(f"перевод не выполнен ({type(exc).__name__}: {str(exc)[:150]}) — "
                                "опубликованы заголовки в оригинале")
        else:
            problems.append("нет ANTHROPIC_API_KEY — заголовки в оригинале")
        if digest is None:
            digest = plain_digest(collected, now.date(), cfg.max_news, cfg.max_events)
    has_content = bool(digest and (digest.news or digest.events or digest.maybe))
    messages = render(digest) if has_content else []
    text = "\n\n— — —\n\n".join(messages)

    write_summary(report(collected, text, problems, now, digest))
    if args.dry_run:
        os.makedirs("preview", exist_ok=True)
        with open("preview/last_dry_run.md", "w", encoding="utf-8") as f:
            f.write(report(collected, text, problems, now, digest))
        return 0

    tg = Telegram(cfg.bot_token)
    stamp = now.isoformat()
    message_id = None
    if has_content:
        message_id = publish(tg, cfg.channel_id, messages, problems)
        try:
            save_archive(cfg.state_path, digest, now)
        except OSError as exc:  # архив — дополнительная функция, публикацию не ломает
            log.warning("Не удалось сохранить архив дайджеста: %s", exc)
        # Показанными считаются только опубликованные анонсы; новости-кандидаты — все,
        # т. к. отобранное и отброшенное Claude не должно всплывать завтра снова.
        for e in digest.events:
            st["announced_events"][e.url] = stamp
    st["seen"].update({u: stamp for u in collected.seen_urls})
    for name in collected.newly_bootstrapped:
        if name not in st["bootstrapped_sources"]:
            st["bootstrapped_sources"].append(name)
    st["last_post_date"] = now.date().isoformat()
    st["history"].append({"date": now.date().isoformat(), "news": len(digest.news) if has_content else 0,
                          "events": len(digest.events) if has_content else 0, "message_id": message_id})
    state_mod.prune(st, now)
    state_mod.save(cfg.state_path, st)

    # Админу пишем, только если что-то не так — как в боте выставок.
    if cfg.admin_chat_id and (problems or not has_content):
        note = [f"🗞 Дайджест {now:%d.%m}: " + ("опубликован." if has_content
                                              else "новых материалов нет, ничего не отправлено.")]
        if problems:
            note.append("\n⚠️ Проблемы:\n" + "\n".join(f"• {p}" for p in problems))
        try:
            tg.send_text(cfg.admin_chat_id, "\n".join(note))
        except TelegramError as exc:
            log.warning("Не удалось написать админу: %s", exc)
    return 0


def cmd_chats(cfg: Config, args) -> int:
    if not cfg.bot_token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    st = state_mod.load(cfg.state_path)
    tg = Telegram(cfg.bot_token)
    me = tg.call("getMe")
    for u in tg.get_updates(st["tg_offset"], 0):
        st["tg_offset"] = max(st["tg_offset"], u["update_id"] + 1)
        chat = (u.get("my_chat_member") or u.get("message") or {}).get("chat") or {}
        if chat.get("id") and chat.get("type") != "private":
            st["known_chats"][str(chat["id"])] = f"{chat.get('title', '')} ({chat.get('type')})"
        member = u.get("my_chat_member", {}).get("new_chat_member", {})
        if member.get("status") in ("left", "kicked"):
            st["known_chats"].pop(str(chat.get("id")), None)
    state_mod.save(cfg.state_path, st)
    lines = [f"## Бот @{me.get('username')}", ""]
    if st["known_chats"]:
        lines += ["Группы и каналы, куда добавлен бот (значение для секрета `CHANNEL_ID`):", ""]
        for cid, title in st["known_chats"].items():
            # GitHub скрывает в логах значения секретов звёздочками, поэтому id,
            # совпадающий с CHANNEL_ID, отображается как ***. Помечаем такой чат явно.
            if cfg.channel_id and cfg.channel_id.strip() == cid:
                lines.append(f"- {title} — ✅ **уже указана в `CHANNEL_ID`**, бот публикует сюда")
            else:
                lines.append(f"- `{cid}` — {title}")
        lines += ["", "_GitHub заменяет на `***` значения секретов: если id показан звёздочками, "
                  "значит он совпадает с одним из секретов._"]
    else:
        lines.append("Бот пока не видит ни одной группы. Добавьте его в группу (или удалите и "
                     "добавьте снова, если он уже там), напишите в группе любое сообщение "
                     "и запустите этот workflow ещё раз.")
    if cfg.channel_id and cfg.channel_id.strip() not in st["known_chats"]:
        lines += ["", "⚠️ Значение `CHANNEL_ID` не совпадает ни с одной из групп выше."]
    write_summary("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest")
    d.add_argument("--force", action="store_true", help="игнорировать расписание и «уже был сегодня»")
    d.add_argument("--dry-run", action="store_true", help="только напечатать, ничего не отправлять")
    d.add_argument("--ignore-seen", action="store_true",
                   help="с --dry-run: брать и уже опубликованные материалы за 3 дня (для настройки отбора)")
    sub.add_parser("chats")
    args = p.parse_args(argv)
    cfg = Config.from_env()
    if args.cmd == "chats":
        return cmd_chats(cfg, args)
    try:
        return cmd_digest(cfg, args)
    except Exception as exc:
        # Причину — сразу админу; workflow всё равно упадёт и пришлёт ссылку на лог.
        if cfg.bot_token and cfg.admin_chat_id and not args.dry_run:
            try:
                Telegram(cfg.bot_token).send_text(
                    cfg.admin_chat_id, f"⚠️ Дайджест не опубликован: {str(exc)[:600]}")
            except Exception:
                log.exception("Не удалось написать админу")
        raise


if __name__ == "__main__":
    sys.exit(main())
