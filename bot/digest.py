"""Отбор, перевод и оформление дайджеста."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

import requests

from .collect import Collected

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TG_LIMIT = 4096
MAX_NEWS_INPUT = 60      # сколько новостей-кандидатов отдаём модели (остальные — в «не вошло»)
MAX_MAYBE = 5            # максимум строк «Возможно, важно»

# Страховочная сетка: слова, по которым новость может касаться быта жителей. Если модель
# такую новость не взяла, она всё равно попадает в пост строкой «Возможно, важно».
IMPORTANT_NL = re.compile(
    r"\b(dicht|gesloten|sluit|sluiting|storing|storingen|afgesloten|afsluiting|omleiding|omgeleid|"
    r"stremming|gestremd|staking|staken|staakt|uitval|valt uit|waarschuw\w*|let op|"
    r"werkzaamheden|wegwerkzaamheden|deadline|uiterlijk|aanvragen|verplicht|boete|"
    r"tarieven|tariefverhoging|verhoging|belasting|afvalinzameling|inzameling|ophaaldag|"
    r"evacuatie|ontruim\w*|noodverordening|kookadvies|drinkwater|stroomstoring)\b",
    re.IGNORECASE)
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
    level: str = "interesting"   # must | interesting | maybe


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
    maybe: list[NewsLine] = field(default_factory=list)    # сетка ключевых слов: модель не взяла
    skipped: list[dict] = field(default_factory=list)      # не вошедшие новости с причиной
    skipped_events: list[dict] = field(default_factory=list)  # не вошедшие анонсы с причиной


SYSTEM_PROMPT = """\
Ты — редактор русскоязычного Telegram-канала о жизни нидерландского города Делфт (Delft).
Каждое утро ты получаешь список свежих местных новостей и ближайших мероприятий
на нидерландском (иногда английском) и готовишь из них дайджест на русском языке.

Правила отбора:
- Новости. Оцени КАЖДУЮ новость из входных данных и присвой уровень (level):
  * "must" — обязательно в выпуск: то, что влияет на быт жителей Делфта. Закрытия, сбои
    и изменения в работе гемеенте и городских служб (приём, документы, сайты, вывоз мусора,
    парковка); перекрытия, ремонт и стройки улиц, изменения маршрутов; забастовки и сбои
    транспорта; происшествия и предупреждения полиции и пожарных, мошенничество с жителями;
    сроки и дедлайны для жителей; изменения цен, налогов, тарифов, правил и страховок,
    которые затрагивают делфтцев. Если сомневаешься между "must" и "interesting" — ставь "must".
  * "interesting" — городская жизнь Делфта: культура, массовые мероприятия, здоровье,
    успехи местных мест и людей в Делфте, новые места и сезонное в городе.
  * "skip" — не брать: то, что происходит НЕ в Делфте (премия делфтцу в Катаре, выставка
    делфтского художника в Сингапуре, Dutch Design Week в Эйндховене); аналитика, колонки,
    интервью, мнения, научно-популярные статьи; внутренняя жизнь TU Delft без влияния на
    жителей; общенациональная политика без конкретного эффекта для Делфта; новости клубов и
    ассоциаций, реклама, партнёрские материалы, конкурсы и розыгрыши; дубликаты.
  Для каждой новости верни title на русском. Для "must" и "interesting" — ещё summary,
  для "skip" — reason: одна короткая фраза по-русски, почему не берём («не в Делфте»,
  «колонка», «реклама», «дубликат»). Если несколько материалов об одном и том же — один
  пункт (с более полной информацией) получает уровень, остальные "skip" с duplicate_of = id
  этого пункта.
  Внутри уровня — по важности. "must" идут в выпуск все; "interesting" — только лучшие,
  до лимита limits.interesting_max.
- Мероприятия: только в Делфте и ближайших окрестностях (Delfgauw, Den Hoorn), проходящие
  в указанный период. Оцени КАЖДОЕ мероприятие из входных данных и присвой уровень (level):
  * "top" — стоит внимания: крупные концерты и спектакли в больших залах (Theater de Veste,
    Nieuwe Kerk, Oude Kerk, Prinsenhof и т. п.), фестивали, ярмарки и рынки, городские праздники,
    значимые выставки (не больше 1–2 длительных), лекции известных людей, спортивные события
    городского масштаба, фестивальное кино и спецпоказы; из церковных — только очень важные
    праздничные службы (Пасха, Рождество).
  * "ok" — можно, если останется место: остальное интересное взрослым жителям.
  * "skip" — не брать: всё детское и школьное (детские мастерские, спектакли и занятия,
    программы для школьников вроде Cultuurbarbaren, каникулярные занятия, семейные утренники);
    выступления местных групп в барах и кафе, open mic, DJ-вечера в барах; обычные церковные
    службы и хоровые вечерни; рутина (обычные сеансы кинотеатров, регулярные консультации и
    кружки — Taalhuis, IDO, Digitale Huiskamer, еженедельные акции баров); события не в
    Делфте (Den Haag и т. п.); дубликаты из разных источников.
  Для "top" и "ok" заполни title, when, date, where, price, summary; для "skip" — только reason
  (одна короткая фраза по-русски: «детское», «бар, местная группа», «не в Делфте», «дубликат»).
  В выпуск идут все "top" (до лимита limits.events_max), оставшиеся места — лучшие "ok".
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

Верни результат, вызвав инструмент submit_digest: в news — ВСЕ новости из входных данных
с уровнями, в events — ВСЕ мероприятия из входных данных с уровнями. id бери ровно из входных данных."""

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
                        "level": {"type": "string", "enum": ["must", "interesting", "skip"]},
                        "title": {"type": "string", "description": "заголовок на русском"},
                        "summary": {"type": "string", "description": "для must и interesting: суть в 1–2 предложениях"},
                        "reason": {"type": "string", "description": "для skip: почему не берём, одна короткая фраза"},
                        "duplicate_of": {"type": "string", "description": "для дубликата: id основного пункта"},
                    },
                    "required": ["id", "level", "title"],
                },
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "level": {"type": "string", "enum": ["top", "ok", "skip"]},
                        "reason": {"type": "string", "description": "для skip: почему не берём, одна короткая фраза"},
                        "title": {"type": "string"},
                        "when": {"type": "string"},
                        "date": {"type": "string", "description": "YYYY-MM-DD, дата начала для сортировки"},
                        "where": {"type": "string"},
                        "price": {"type": "string"},
                        "summary": {"type": "string", "description": "одно короткое предложение: что это за событие (жанр, о чём, для кого); пусто, только если во входных данных совсем нет сведений"},
                    },
                    "required": ["id", "level"],
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


def stable_order(c: Collected) -> None:
    """Одинаковый порядок кандидатов при одинаковых данных — предсказуемый отбор."""
    c.news.sort(key=lambda n: (-(n.published.timestamp()) if n.published else 0, n.source, n.url))
    c.events.sort(key=lambda e: (e.start.isoformat() if e.start else "9999", e.source, e.url))


def build_input(c: Collected, now: datetime, horizon_end: date, max_news: int, max_events: int) -> str:
    payload = {
        "today": now.date().isoformat(),
        "weekday": WEEKDAYS[now.weekday()],
        "events_period": f"{now.date().isoformat()} .. {horizon_end.isoformat()}",
        "limits": {"interesting_max": max_news, "events_max": max_events},
        "news": [
            {"id": f"n{i}", "source": n.source, "title": n.title,
             "published": n.published.isoformat() if n.published else None, "text": n.text}
            for i, n in enumerate(c.news[:MAX_NEWS_INPUT])
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
    stable_order(c)
    body = {
        "model": model,
        "max_tokens": 16000,  # с запасом: часть токенов может уйти на «размышление» модели
        "temperature": 0,     # предсказуемый отбор: одинаковые данные — одинаковый результат
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
        if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
            log.info("Модель не принимает temperature — повтор без него")
            body = {k: v for k, v in body.items() if k != "temperature"}
            continue
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


def _start_time(when: str) -> str:
    import re
    m = re.search(r"\b(\d{1,2}):(\d{2})", when or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _flagged(n) -> bool:
    return bool(IMPORTANT_NL.search(f"{n.title} {n.text}"))


def _skip(n, title: str, reason: str) -> dict:
    return {"title": title or n.title, "original": n.title, "url": n.url, "source": n.source,
            "reason": reason, "flagged": _flagged(n)}


def _maybe_lines(skipped: list[dict]) -> list[NewsLine]:
    out = []
    for s in skipped:
        if s["flagged"] and not s.get("duplicate"):
            out.append(NewsLine(s["title"], "", s["url"], s["source"], "maybe"))
    return out[:MAX_MAYBE]


def digest_from_llm(result: dict, c: Collected, day: date, max_news: int, max_events: int) -> Digest:
    """Собирает дайджест из ответа модели; ссылки берутся только из наших данных.
    must — все; interesting — до лимита; всё отброшенное сохраняется в skipped с причиной."""
    news_by_id = {f"n{i}": n for i, n in enumerate(c.news)}
    events_by_id = {f"e{i}": e for i, e in enumerate(c.events)}
    must, interesting, skipped, used, rated = [], [], [], set(), set()
    for item in result.get("news", []):
        nid = str(item.get("id"))
        n = news_by_id.get(nid)
        if not n or nid in rated:
            continue
        rated.add(nid)
        level = str(item.get("level") or "interesting").lower()
        title = (item.get("title") or "").strip() or n.title
        if level == "skip" or n.url in used:
            entry = _skip(n, title, (item.get("reason") or "").strip() or ("дубликат" if n.url in used else "не подходит"))
            if item.get("duplicate_of") or n.url in used:
                entry["duplicate"] = True
            skipped.append(entry)
            continue
        used.add(n.url)
        line = NewsLine(title, (item.get("summary") or "").strip(), n.url, n.source,
                        "must" if level == "must" else "interesting")
        (must if line.level == "must" else interesting).append(line)
    cap = max(3, max_news - len(must))
    for line in interesting[cap:]:
        n = next(x for x in c.news if x.url == line.url)
        skipped.append(_skip(n, line.title, "не хватило места в выпуске"))
    for nid, n in news_by_id.items():
        if nid not in rated and n.url not in used:
            skipped.append(_skip(n, "", "модель не оценила" if int(nid[1:]) < MAX_NEWS_INPUT
                                 else "слишком много новостей за день — не отдана модели"))
    news = must + interesting[:cap]
    used = {x.url for x in news}
    top, ok, skipped_events, rated_e = [], [], [], set()

    def skip_event(e, reason):
        skipped_events.append({"title": e.title, "url": e.url, "source": e.source, "reason": reason,
                               "date": e.start.isoformat() if e.start else ""})

    for item in result.get("events", []):
        eid = str(item.get("id"))
        e = events_by_id.get(eid)
        if not e or eid in rated_e:
            continue
        rated_e.add(eid)
        level = str(item.get("level") or "ok").lower()
        if level == "skip" or e.url in used:
            skip_event(e, (item.get("reason") or "").strip() or ("дубликат" if e.url in used else "не подходит"))
            continue
        used.add(e.url)
        line = EventLine(item.get("title", "").strip() or e.title,
                         item.get("when", "").strip() or _event_when_fallback(e.start, e.end, e.time_text),
                         (item.get("where") or "").strip(), (item.get("price") or "").strip(),
                         (item.get("summary") or "").strip(), e.url,
                         item.get("date") or (e.start.isoformat() if e.start else ""))
        (top if level == "top" else ok).append((line, e))
    chosen = (top + ok)[:max_events]
    for line, e in (top + ok)[max_events:]:
        skip_event(e, "не хватило места в выпуске")
    for eid, e in events_by_id.items():
        if eid not in rated_e and e.url not in used:
            skip_event(e, "модель не оценила")
    events = [line for line, _ in chosen]
    # по дате, внутри дня — по времени начала («весь день» и без времени — в начале дня)
    events.sort(key=lambda x: (x.sort_key, _start_time(x.when)))
    return Digest(day, news, events, _maybe_lines(skipped), skipped, skipped_events)


def plain_digest(c: Collected, day: date, max_news: int, max_events: int) -> Digest:
    """Запасной вариант без LLM: оригинальные заголовки без перевода."""
    stable_order(c)
    news = [NewsLine(n.title, "", n.url, n.source) for n in c.news[:max_news]]
    skipped = [_skip(n, "", "без перевода: не хватило места") for n in c.news[max_news:]]
    # разовые события вперёд, длительные (начавшиеся раньше) — в конец
    ordered = sorted(c.events, key=lambda e: (e.start is None or e.start < day, e.start or day))
    events = [EventLine(e.title, _event_when_fallback(e.start, e.end, e.time_text), e.venue,
                        e.price, "", e.url, e.start.isoformat() if e.start else "")
              for e in ordered[:max_events]]
    return Digest(day, news, events, _maybe_lines(skipped), skipped)


def digest_to_dict(d: Digest, published: datetime) -> dict:
    """Дайджест в JSON — для внешних читателей (например, виджета на домашней странице)."""
    return {
        "version": 1,
        "date": d.day.isoformat(),
        "title": f"Делфт · {ru_date(d.day)}",
        "published": published.isoformat(),
        "news": [{"title": n.title, "summary": n.summary, "url": n.url, "source": n.source,
                  "level": n.level} for n in d.news],
        "maybe": [{"title": n.title, "url": n.url, "source": n.source} for n in d.maybe],
        "skipped": [{k: v for k, v in s.items() if k != "duplicate"} for s in d.skipped],
        "skipped_events": d.skipped_events,
        "events": [{"title": e.title, "when": e.when, "where": e.where, "price": e.price,
                    "summary": e.summary, "url": e.url, "date": e.sort_key} for e in d.events],
    }


def render_news(n: NewsLine) -> str:
    """<b>Заголовок</b> — <a>Источник</a>\n<i>Краткое содержание</i>"""
    head = f'<b>{escape(n.title)}</b> — <a href="{escape(n.url, quote=True)}">{escape(n.source)}</a>'
    return f"{head}\n<i>{escape(n.summary)}</i>" if n.summary else head


def render_event(e: EventLine) -> str:
    """дата/время — <a>Название</a>\n<i>место · цена. Краткое содержание</i>"""
    link = f'<a href="{escape(e.url, quote=True)}">{escape(e.title)}</a>'
    head = f"{escape(e.when)} — {link}" if e.when else link
    place = " · ".join(x for x in (e.where, e.price) if x)
    summary = e.summary.strip().rstrip(".")
    body = ". ".join(x for x in (place, summary) if x)
    if summary:
        body += "."
    return f"{head}\n<i>{escape(body)}</i>" if body else head


def render(d: Digest) -> list[str]:
    """Дайджест в HTML для Telegram; длинный — несколько сообщений по границам пунктов."""
    blocks = [f"<b>🗞 Делфт · {escape(ru_date(d.day))}</b>"]
    if d.news or d.maybe:
        blocks.append("<b>📰 События</b>")
        blocks.extend(render_news(n) for n in d.news)
    if d.maybe:
        blocks.append("<i>Возможно, важно:</i>\n" + "\n".join(
            f'• {escape(n.title)} — <a href="{escape(n.url, quote=True)}">{escape(n.source)}</a>' for n in d.maybe))
    if d.events:
        blocks.append("<b>📅 Анонсы</b>")
        blocks.extend(render_event(e) for e in d.events)
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > TG_LIMIT and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
