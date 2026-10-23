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
import os
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


def dry_run_report(collected, note: str, text: str, now: datetime) -> str:
    """Отчёт пробного прогона: служебная заметка, сам пост и все кандидаты."""
    lines = [f"# Пробный прогон {now:%Y-%m-%d %H:%M}", "", "## Служебное сообщение", "",
             "```", note, "```", "", "## Пост (HTML для Telegram)", "", "```html", text, "```", "",
             f"## Кандидаты: новости ({len(collected.news)})", ""]
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


def cmd_digest(cfg: Config, args) -> int:
    now = datetime.now(cfg.tz)
    st = state_mod.load(cfg.state_path)
    today = now.date().isoformat()
    if not args.force and not args.dry_run:
        if st.get("last_draft_date") == today:
            log.info("Черновик на %s уже был, пропускаю", today)
            return 0
        # Расписание GitHub может опаздывать на часы или пропускать запуски,
        # поэтому черновик готовится при первом запуске в окне DRAFT_HOUR..MODERATION_UNTIL_HOUR.
        if not (cfg.draft_hour <= now.hour < max(cfg.moderation_until_hour, cfg.draft_hour + 3)):
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
        report = dry_run_report(collected, service_note(collected, used_llm, llm_error, now), text, now)
        print(report)
        os.makedirs("preview", exist_ok=True)
        with open("preview/last_dry_run.md", "w", encoding="utf-8") as f:
            f.write(report)
        return 0

    tg = Telegram(cfg.bot_token)
    save = lambda: state_mod.save(cfg.state_path, st)  # noqa: E731
    mod = Moderator(cfg, tg, st, save, lambda: datetime.now(cfg.tz))
    mod.poll()  # сначала разбираем накопившиеся обновления, чтобы не перепутать черновики

    # Новости-кандидаты отмечаются показанными только после публикации (см. Moderator.publish),
    # чтобы отклонённый или пересобранный черновик не «съедал» их. Остальные ссылки — сразу.
    news_urls = [n.url for n in collected.news]
    st["seen"].update({u: now.isoformat() for u in collected.seen_urls if u not in set(news_urls)})
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
                     [e.url for e in digest.events], news_urls)
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
    me = tg.call("getMe")
    mod = Moderator(cfg, tg, st, lambda: state_mod.save(cfg.state_path, st),
                    lambda: datetime.now(cfg.tz))
    mod.poll()
    state_mod.save(cfg.state_path, st)
    received = getattr(mod, "processed", 0)

    lines = [f"## Бот @{me.get('username')}", "",
             f"Новых сообщений и нажатий: **{received}**.", ""]
    if cfg.admin_chat_id is None:
        if received:
            lines.append("`ADMIN_CHAT_ID` не задан — бот ответил отправителям их chat_id в Telegram.")
        else:
            lines.append(f"`ADMIN_CHAT_ID` не задан, а сообщений нет. Откройте https://t.me/{me.get('username')}, "
                         "нажмите **Start** (или отправьте любое сообщение) и запустите Moderate ещё раз.")
    else:
        lines.append(f"Модератор: `{cfg.admin_chat_id}`. {mod.status()}")
    summary = "\n".join(lines) + "\n"
    log.info(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)
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
