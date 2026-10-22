"""Отбор, перевод и оформление дайджеста."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from html import escape

import requests

from .collect import Collected

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TG_LIMIT = 4096
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


@dataclass
class NewsLine:
    title: str
    summary: str
    url: str
    source: str


@dataclass
class EventLine:
    title: str
    when: str
    where: str
    price: str
    summary: str
    url: str
    sort_key: str


@dataclass
class Digest:
    day: date
    news: list[NewsLine]
    events: list[EventLine]


SYSTEM_PROMPT = """\
Ты — редактор русскоязычного Telegram-канала о жизни нидерландского города Делфт (Delft).
Каждое утро ты получаешь список свежих местных новостей и ближайших мероприятий
на нидерландском (иногда английском) и готовишь из них дайджест на русском языке.

Правила отбора:
- Новости: включай всё, что важно или интересно жителям Делфта (город, районы, транспорт,
  стройки, происшествия, решения гемеенте, культура, TU Delft — если это касается города).
  Пропускай рекламу, партнёрские материалы, конкурсы, гороскопы и общенациональные новости
  без местной привязки. Если несколько материалов об одном и том же — оставь один
  (предпочтительно с более полной информацией).
- Мероприятия: только в Делфте и ближайших окрестностях (Delfgauw, Den Hoorn), проходящие
  в указанный период; события из других городов (Den Haag и т. п.) не включай.
  Выбирай самое интересное для широкой публики и делай подборку разнообразной:
  концерты, спектакли, фестивали, ярмарки, лекции, городские праздники, спорт, выставки.
  Не больше 1–2 длительных выставок и не больше 2 детских мероприятий.
  Не включай рутину: обычные сеансы кинотеатров (только фестивальные и спецпоказы),
  регулярные консультации и кружки (Taalhuis, IDO, Digitale Huiskamer, чтения для малышей),
  ежедневные/еженедельные «ночные» акции баров. Объединяй дубликаты из разных источников.
- Ничего не выдумывай: используй только факты из входных данных. Если нет места, цены или
  времени — оставь поле пустым.

Стиль:
- Заголовок новости: короткий, информативный, по-русски, без кликбейта (до ~90 знаков).
- Суть: 1–2 простых предложения с главным фактом (до ~220 знаков).
- Названия улиц, заведений, организаций, районов пиши в оригинале латиницей
  (Markt, Theater de Veste, TU Delft, Tanthof). Название мероприятия переводи,
  если оно описательное; имена артистов и названия спектаклей/фильмов оставляй как есть.
- «when» для мероприятия: в формате «сб 24 окт, 20:15» или «до 1 нояб.» для длительных.
- Цена: «бесплатно», «€15» и т. п.

Верни результат, вызвав инструмент submit_digest. Порядок новостей — по важности,
мероприятий — по дате. id бери ровно из входных данных."""

TOOL = {
    "name": "submit_digest",
    "description": "Передать готовый дайджест на русском языке.",
    "input_schema": {
        "type": "object",
        "properties": {
            "news": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["id", "title", "summary"],
                },
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "when": {"type": "string"},
                        "date": {"type": "string", "description": "YYYY-MM-DD, дата начала для сортировки"},
                        "where": {"type": "string"},
                        "price": {"type": "string"},
                        "summary": {"type": "string", "description": "одно короткое предложение или пусто"},
                    },
                    "required": ["id", "title", "when", "date"],
                },
            },
        },
        "required": ["news", "events"],
    },
}


def ru_date(d: date, weekday: bool = True) -> str:
    text = f"{d.day} {MONTHS_GEN[d.month - 1]}"
    return f"{WEEKDAYS[d.weekday()]}, {text}" if weekday else text


def _event_when_fallback(start: date | None, end: date | None, time_text: str) -> str:
    if not start:
        return ""
    s = f"{WEEKDAYS_SHORT[start.weekday()]} {start.day} {MONTHS_GEN[start.month - 1][:3]}"
    if end and end != start:
        s = f"{s} – {end.day} {MONTHS_GEN[end.month - 1][:3]}"
    return f"{s}, {time_text}" if time_text and len(time_text) <= 13 else s


def build_input(c: Collected, now: datetime, horizon_end: date, max_news: int, max_events: int) -> str:
    payload = {
        "today": now.date().isoformat(),
        "weekday": WEEKDAYS[now.weekday()],
        "events_period": f"{now.date().isoformat()} .. {horizon_end.isoformat()}",
        "limits": {"news_max": max_news, "events_max": max_events},
        "news": [
            {"id": f"n{i}", "source": n.source, "title": n.title,
             "published": n.published.isoformat() if n.published else None, "text": n.text}
            for i, n in enumerate(c.news[:30])
        ],
        "events": [
            {"id": f"e{i}", "source": e.source, "title": e.title[:200],
             "start": e.start.isoformat() if e.start else None,
             "end": e.end.isoformat() if e.end else None,
             "time": e.time_text, "venue": e.venue, "price": e.price, "text": e.text[:250]}
            for i, e in enumerate(c.events[:150])
        ],
    }
    return ("Подготовь дайджест на сегодня. Входные данные (JSON):\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))


def llm_digest(c: Collected, now: datetime, horizon_end: date, api_key: str, model: str,
               max_news: int, max_events: int) -> Digest:
    body = {
        "model": model,
        "max_tokens": 16000,  # с запасом: часть токенов может уйти на «размышление» модели
        "system": SYSTEM_PROMPT,
        "tools": [TOOL],
        # Принудительный вызов инструмента ("tool") поддерживается не всеми моделями
        # (например, с включённым размышлением) — при отказе API повторяем с "auto".
        "tool_choice": {"type": "tool", "name": "submit_digest"},
        "messages": [{"role": "user", "content": build_input(c, now, horizon_end, max_news, max_events)
                      + "\n\nОбязательно верни результат вызовом инструмента submit_digest."}],
    }
    content = _call_claude(body, api_key)
    result = extract_result(content)
    if result is None:
        raise RuntimeError("Модель не вернула дайджест")
    return digest_from_llm(result, c, now.date(), max_news, max_events)


def _call_claude(body: dict, api_key: str) -> list[dict]:
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    attempt = 0
    while True:
        r = requests.post(ANTHROPIC_URL, json=body, headers=headers, timeout=300)
        if r.status_code == 400 and "tool_choice" in r.text and body.get("tool_choice", {}).get("type") != "auto":
            log.info("Модель не поддерживает принудительный tool_choice — повтор с auto")
            body = {**body, "tool_choice": {"type": "auto"}}
            continue
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 3:
            attempt += 1
            log.warning("Claude API %s, повтор", r.status_code)
            time.sleep(10 * attempt)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
        return r.json().get("content", [])


def extract_result(content: list[dict]) -> dict | None:
    """Дайджест из ответа: вызов инструмента или, на крайний случай, JSON в тексте."""
    for block in content:
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            return block["input"]
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and ("news" in data or "events" in data):
            return data
    return None


def digest_from_llm(result: dict, c: Collected, day: date, max_news: int, max_events: int) -> Digest:
    """Собирает дайджест из ответа модели; ссылки берутся только из наших данных."""
    news_by_id = {f"n{i}": n for i, n in enumerate(c.news)}
    events_by_id = {f"e{i}": e for i, e in enumerate(c.events)}
    news, used = [], set()
    for item in result.get("news", []):
        n = news_by_id.get(str(item.get("id")))
        if not n or n.url in used:
            continue
        used.add(n.url)
        news.append(NewsLine(item.get("title", "").strip() or n.title,
                             item.get("summary", "").strip(), n.url, n.source))
    events = []
    for item in result.get("events", []):
        e = events_by_id.get(str(item.get("id")))
        if not e or e.url in used:
            continue
        used.add(e.url)
        events.append(EventLine(item.get("title", "").strip() or e.title,
                                item.get("when", "").strip() or _event_when_fallback(e.start, e.end, e.time_text),
                                (item.get("where") or "").strip(), (item.get("price") or "").strip(),
                                (item.get("summary") or "").strip(), e.url,
                                item.get("date") or (e.start.isoformat() if e.start else "")))
    events.sort(key=lambda x: x.sort_key)
    return Digest(day, news[:max_news], events[:max_events])


def plain_digest(c: Collected, day: date, max_news: int, max_events: int) -> Digest:
    """Запасной вариант без LLM: оригинальные заголовки без перевода."""
    news = [NewsLine(n.title, "", n.url, n.source) for n in c.news[:max_news]]
    # разовые события вперёд, длительные (начавшиеся раньше) — в конец
    ordered = sorted(c.events, key=lambda e: (e.start is None or e.start < day, e.start or day))
    events = [EventLine(e.title, _event_when_fallback(e.start, e.end, e.time_text), e.venue,
                        e.price, "", e.url, e.start.isoformat() if e.start else "")
              for e in ordered[:max_events]]
    return Digest(day, news, events)


def render(d: Digest) -> str:
    """HTML для Telegram (parse_mode=HTML). Укладывается в лимит 4096 символов."""
    news, events = list(d.news), list(d.events)
    while True:
        text = _render(d.day, news, events)
        if len(text) <= TG_LIMIT or not (news or events):
            return text
        # убираем по одному пункту с конца более длинного раздела
        (news if len(news) >= len(events) else events).pop()


def _render(day: date, news: list[NewsLine], events: list[EventLine]) -> str:
    parts = [f"<b>🗞 Делфт · {escape(ru_date(day))}</b>"]
    if news:
        parts.append("\n<b>📰 События</b>")
        for n in news:
            line = f"• <b>{escape(n.title)}</b>"
            if n.summary:
                line += f" — {escape(n.summary)}"
            line += f' <a href="{escape(n.url, quote=True)}">{escape(n.source)}</a>'
            parts.append(line)
    if events:
        parts.append("\n<b>📅 Анонсы</b>")
        for e in events:
            line = f"• <b>{escape(e.when)}</b> · {escape(e.title)}" if e.when else f"• {escape(e.title)}"
            extra = ", ".join(x for x in (e.where, e.price) if x)
            if extra:
                line += f" ({escape(extra)})"
            if e.summary:
                line += f". {escape(e.summary.rstrip('.'))}."
            line += f' <a href="{escape(e.url, quote=True)}">→</a>'
            parts.append(line)
    return "\n".join(parts)
