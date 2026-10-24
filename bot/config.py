"""Настройки из переменных окружения (секреты и переменные GitHub Actions)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value else default


@dataclass
class Config:
    bot_token: str
    admin_chat_id: int | None
    channel_id: str
    anthropic_key: str
    model: str
    tz: ZoneInfo
    post_hour: int               # с какого часа (местного) публиковать
    post_until_hour: int         # до какого часа ещё можно опубликовать, если расписание опоздало
    news_window_hours: int       # насколько свежими должны быть новости
    event_horizon_days: int      # на сколько дней вперёд брать анонсы
    max_news: int
    max_events: int
    state_path: str
    sources_path: str

    @classmethod
    def from_env(cls) -> "Config":
        admin = _str("ADMIN_CHAT_ID")
        return cls(
            bot_token=_str("TELEGRAM_BOT_TOKEN"),
            admin_chat_id=int(admin) if admin else None,
            channel_id=_str("CHANNEL_ID") or _str("CHAT_ID"),
            anthropic_key=_str("ANTHROPIC_API_KEY"),
            model=_str("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            tz=ZoneInfo(_str("TIMEZONE", "Europe/Amsterdam")),
            post_hour=_int("POST_HOUR", _int("DRAFT_HOUR", 6)),
            post_until_hour=_int("POST_UNTIL_HOUR", 12),
            news_window_hours=_int("NEWS_WINDOW_HOURS", 30),
            event_horizon_days=_int("EVENT_HORIZON_DAYS", 3),
            max_news=_int("MAX_NEWS", 8),
            max_events=_int("MAX_EVENTS", 8),
            state_path=_str("STATE_PATH", "state/state.json"),
            sources_path=_str("SOURCES_PATH", "sources.yaml"),
        )

    def require_telegram(self) -> None:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", self.bot_token),
                                  ("CHANNEL_ID", self.channel_id)) if not v]
        if missing:
            raise SystemExit(f"Не заданы секреты: {', '.join(missing)}")
