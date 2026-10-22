"""Точка входа.

    python -m bot digest [--force] [--dry-run] [--no-wait]
        Собрать новости, отправить черновик модератору и (по умолчанию) ждать решения
        до MODERATION_UNTIL_HOUR. --dry-run печатает дайджест и ничего не отправляет.

    python -m bot moderate
        Обработать накопившиеся нажатия кнопок и сообщения модератора.
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys
from datetime import datetime, timedelta

from . import state as state_mod
from .collect import collect, load_sources
from .config import Config
from .digest import llm_digest, plain_digest, render
from .fetch import Fetcher
from .moderation import Moderator
from .telegram import Telegram

log = logging.getLogger("bot")


def service_note(collected, used_llm: bool, llm_error: str | None, now: datetime) -> str:
    lines = [f"📝 Черновик дайджеста на {now:%d.%m}. Проверьте и нажмите кнопку под ним.",
             "Чтобы поправить — ответьте на черновик исправленным текстом."]
    counts = ", ".join(f"{k}: {v}" for k, v in collected.per_source.items())
    if counts:
        lines.append(f"\nНайдено: {counts}.")
    if collected.newly_bootstrapped:
        lines.append("Первый запуск для: " + ", ".join(collected.newly_bootstrapped)
                     + " — взяты только материалы с явной свежей датой.")
    if collected.errors:
        lines.append("\n⚠️ Проблемы с источниками:\n" + "\n".join(f"• {e}" for e in collected.errors))
    if not used_llm:
        lines.append("\n⚠️ Перевод не выполнен" + (f" ({llm_error})" if llm_error else
                     " (нет ANTHROPIC_API_KEY)") + " — заголовки в оригинале.")
    return "\n".join(lines)


def cmd_digest(cfg: Config, args) -> int:
    now = datetime.now(cfg.tz)
    st = state_mod.load(cfg.state_path)
    today = now.date().isoformat()
    if not args.force and not args.dry_run:
        if st.get("last_draft_date") == today:
            log.info("Черновик на %s уже был, пропускаю", today)
            return 0
        if not (cfg.draft_hour <= now.hour < cfg.draft_hour + 3):
            log.info("Сейчас %s по местному времени — не время для черновика (DRAFT_HOUR=%s)",
                     now.strftime("%H:%M"), cfg.draft_hour)
            return 0
    if not args.dry_run:
        cfg.require_telegram()
        if cfg.admin_chat_id is None:
            raise SystemExit("Не задан ADMIN_CHAT_ID. Напишите боту /start и запустите workflow «Moderate».")

    sources = load_sources(cfg.sources_path)
    collected = collect(Fetcher(), st, now, sources, cfg.news_window_hours, cfg.event_horizon_days)
    log.info("Собрано: новостей %d, событий %d, ошибок %d",
             len(collected.news), len(collected.events), len(collected.errors))
    horizon_end = now.date() + timedelta(days=cfg.event_horizon_days)

    used_llm, llm_error = False, None
    if cfg.anthropic_key and (collected.news or collected.events):
        try:
            digest = llm_digest(collected, now, horizon_end, cfg.anthropic_key, cfg.model,
                                cfg.max_news, cfg.max_events)
            used_llm = True
        except Exception as exc:
            log.exception("LLM не сработал")
            llm_error = f"{type(exc).__name__}: {str(exc)[:120]}"
    if not used_llm:
        digest = plain_digest(collected, now.date(), cfg.max_news, cfg.max_events)
    text = render(digest)

    if args.dry_run:
        print(service_note(collected, used_llm, llm_error, now))
        print("\n" + "-" * 40 + "\n" + text)
        return 0

    tg = Telegram(cfg.bot_token)
    save = lambda: state_mod.save(cfg.state_path, st)  # noqa: E731
    mod = Moderator(cfg, tg, st, save, lambda: datetime.now(cfg.tz))
    mod.poll()  # сначала разбираем накопившиеся обновления, чтобы не перепутать черновики

    st["seen"].update({u: now.isoformat() for u in collected.seen_urls})
    for name in collected.newly_bootstrapped:
        if name not in st["bootstrapped_sources"]:
            st["bootstrapped_sources"].append(name)
    st["last_draft_date"] = today
    state_mod.prune(st, now)

    if not digest.news and not digest.events:
        mod.supersede()
        note = f"📭 {now:%d.%m}: новых материалов нет, черновика сегодня не будет."
        if collected.errors:
            note += "\n\n⚠️ Проблемы с источниками:\n" + "\n".join(f"• {e}" for e in collected.errors)
        tg.send_text(cfg.admin_chat_id, note)
        save()
        return 0

    draft_id = f"{now:%Y%m%d}-{secrets.token_hex(2)}"
    mod.submit_draft(draft_id, {"text": text, "parse_mode": "HTML"},
                     service_note(collected, used_llm, llm_error, now),
                     [e.url for e in digest.events])
    log.info("Черновик %s отправлен модератору", draft_id)

    if not args.no_wait:
        until = now.replace(hour=cfg.moderation_until_hour, minute=0, second=0, microsecond=0)
        if until > datetime.now(cfg.tz):
            log.info("Жду решения модератора до %s", until.strftime("%H:%M"))
            mod.poll(until=until)
    save()
    return 0


def cmd_moderate(cfg: Config, args) -> int:
    if not cfg.bot_token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    st = state_mod.load(cfg.state_path)
    tg = Telegram(cfg.bot_token)
    mod = Moderator(cfg, tg, st, lambda: state_mod.save(cfg.state_path, st),
                    lambda: datetime.now(cfg.tz))
    mod.poll()
    state_mod.save(cfg.state_path, st)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest")
    d.add_argument("--force", action="store_true", help="игнорировать расписание и «уже был сегодня»")
    d.add_argument("--dry-run", action="store_true", help="только напечатать, ничего не отправлять")
    d.add_argument("--no-wait", action="store_true", help="не ждать решения модератора")
    sub.add_parser("moderate")
    args = p.parse_args(argv)
    cfg = Config.from_env()
    return cmd_digest(cfg, args) if args.cmd == "digest" else cmd_moderate(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
