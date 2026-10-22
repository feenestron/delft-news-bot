"""Сбор кандидатов в дайджест из всех источников."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import yaml

from .fetch import Fetcher, clean_text, extract_links, find_dutch_dates, page_details, parse_feed

log = logging.getLogger(__name__)


@dataclass
class News:
    source: str
    title: str
    url: str
    text: str = ""
    published: datetime | None = None


@dataclass
class Event:
    source: str
    title: str
    url: str
    start: date | None = None
    end: date | None = None
    time_text: str = ""
    venue: str = ""
    price: str = ""
    text: str = ""


@dataclass
class Collected:
    news: list[News] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seen_urls: list[str] = field(default_factory=list)
    newly_bootstrapped: list[str] = field(default_factory=list)
    per_source: dict[str, int] = field(default_factory=dict)


def load_sources(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {k: [s for s in data.get(k, []) if s.get("enabled", True)] for k in ("news", "events")}


def _short_error(exc: Exception) -> str:
    return clean_text(f"{type(exc).__name__}: {exc}", 160)


def _aware(dt: datetime | None, tz) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


class Collector:
    def __init__(self, fetcher: Fetcher, state: dict, now: datetime,
                 news_window_hours: int, event_horizon_days: int):
        self.f = fetcher
        self.state = state
        self.now = now
        self.tz = now.tzinfo
        self.today = now.date()
        self.horizon_end = self.today + timedelta(days=event_horizon_days)
        self.news_cutoff = now - timedelta(hours=news_window_hours)
        self.out = Collected()

    # ------------------------------------------------------------------ news
    def _fresh(self, published: datetime | None) -> bool:
        return published is None or published >= self.news_cutoff

    def news_rss(self, src: dict) -> list[News]:
        entries = parse_feed(self.f.get(src["url"]).content)
        items = []
        for e in entries:
            url = e["link"]
            if not url or url in self.state["seen"]:
                continue
            published = e["published"]
            if published is not None and published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(self.tz) if published else None
            self.out.seen_urls.append(url)
            if published is None or published < self.news_cutoff:
                continue
            items.append(News(src["name"], e["title"], url, e["summary"], published))
        return items

    def news_html(self, src: dict) -> list[News]:
        listing = self.f.get(src["url"]).text
        links = extract_links(listing, src["url"], src["link_pattern"],
                              src.get("exclude_pattern"), src.get("scope_selector"))
        if not links:
            raise ValueError("на странице не найдено ни одной подходящей ссылки — проверьте link_pattern")
        bootstrapped = src["name"] in self.state["bootstrapped_sources"]
        new = [(u, t) for u, t, _ in links if u not in self.state["seen"]]
        items = []
        for url, anchor in new[: src.get("max_fetch", 12)]:
            try:
                d = page_details(self.f.get(url).text)
            except Exception as exc:  # одна битая страница не должна ломать источник
                log.warning("%s: %s", url, exc)
                continue
            published = _aware(d["published"], self.tz)
            # Первый запуск: всё на странице уже "старое", кроме материалов с явной свежей датой.
            if not bootstrapped and published is None:
                continue
            if not self._fresh(published):
                continue
            items.append(News(src["name"], d["title"] or anchor, url,
                              d["description"] or d["text"][:600], published))
        self.out.seen_urls.extend(u for u, _ in new)
        if not bootstrapped:
            self.out.newly_bootstrapped.append(src["name"])
        return items

    # ---------------------------------------------------------------- events
    def _in_horizon(self, start: date | None, end: date | None) -> bool:
        if start is None:
            return False
        return start <= self.horizon_end and (end or start) >= self.today

    def _not_announced(self, url: str) -> bool:
        return url not in self.state["announced_events"]

    def events_tribe(self, src: dict) -> list[Event]:
        base = src["url"].rstrip("/")
        params = {"per_page": 50, "start_date": self.today.isoformat(),
                  "end_date": f"{self.horizon_end.isoformat()} 23:59:59"}
        data = self.f.get(f"{base}/wp-json/tribe/events/v1/events", params=params,
                          headers={"Accept": "application/json"}).json()
        cities = {c.lower() for c in src.get("cities", [])}
        items = []
        for e in data.get("events", []):
            url = e.get("url")
            if not url or not self._not_announced(url):
                continue
            venue = e.get("venue") if isinstance(e.get("venue"), dict) else {}
            city = (venue.get("city") or "").strip().lower()
            if cities and city and city not in cities:
                continue
            start = datetime.fromisoformat(e["start_date"]) if e.get("start_date") else None
            end = datetime.fromisoformat(e["end_date"]) if e.get("end_date") else None
            if not start or not self._in_horizon(start.date(), end.date() if end else None):
                continue
            time_text = "весь день" if e.get("all_day") else start.strftime("%H:%M")
            if end and not e.get("all_day") and end.date() == start.date():
                time_text += "–" + end.strftime("%H:%M")
            venue_text = ", ".join(x for x in (clean_text(venue.get("venue")), venue.get("address")) if x)
            items.append(Event(src["name"], clean_text(e.get("title")), url, start.date(),
                               end.date() if end else None, time_text, venue_text,
                               clean_text(e.get("cost")),
                               clean_text(e.get("excerpt") or e.get("description"), 500)))
        return items

    def events_html(self, src: dict) -> list[Event]:
        listing = self.f.get(src["url"]).text
        links = extract_links(listing, src["url"], src["link_pattern"],
                              src.get("exclude_pattern"), src.get("scope_selector"))
        if not links:
            raise ValueError("на странице не найдено ни одной подходящей ссылки — проверьте link_pattern")
        has_url_date = "(?P<d>" in src["link_pattern"]
        cache = self.state["event_cache"]
        budget = src.get("max_fetch", 15)
        items = []
        for url, anchor, m in links:
            if not self._not_announced(url):
                continue
            start = end = None
            if has_url_date:
                try:
                    start = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
                except ValueError:
                    continue
                if not self._in_horizon(start, None):
                    continue
            cached = cache.get(url)
            if cached is None and budget > 0:
                budget -= 1
                try:
                    d = page_details(self.f.get(url).text, src.get("scope_selector"))
                except Exception as exc:
                    log.warning("%s: %s", url, exc)
                    d = None
                if d:
                    # 1) блок «Wanneer/Datum» на странице — самый надёжный;
                    # 2) schema.org Event (у некоторых сайтов там период показа объявления);
                    # 3) первая дата в тексте.
                    s, e, snippet = find_dutch_dates(d["full_text"], self.today, anchored_only=True)
                    if not s and d["event_start"]:
                        s, e = d["event_start"], d["event_end"]
                        s = s.date() if isinstance(s, datetime) else s
                        e = e.date() if isinstance(e, datetime) else e
                    if not s:
                        s, e, snippet = find_dutch_dates(d["full_text"], self.today)
                    cached = {
                        "title": d["title"], "venue": d["venue"],
                        "text": d["description"] or d["text"][:500],
                        "start": s.isoformat() if s else None,
                        "end": e.isoformat() if e else None,
                        "date_text": snippet,
                        "fetched": self.now.isoformat(),
                    }
                    cache[url] = cached
            if not has_url_date:
                if not cached or not cached.get("start"):
                    continue
                start = date.fromisoformat(cached["start"])
                end = date.fromisoformat(cached["end"]) if cached.get("end") else None
                if not self._in_horizon(start, end):
                    continue
            cached = cached or {}
            items.append(Event(src["name"], cached.get("title") or anchor, url, start, end,
                               cached.get("date_text", ""), cached.get("venue", ""), "",
                               cached.get("text", "")))
        return items

    # ------------------------------------------------------------------ main
    def run(self, sources: dict) -> Collected:
        handlers = {
            ("news", "rss"): self.news_rss,
            ("news", "html_links"): self.news_html,
            ("events", "tribe_events"): self.events_tribe,
            ("events", "html_links"): self.events_html,
        }
        for kind in ("news", "events"):
            for src in sources.get(kind, []):
                handler = handlers.get((kind, src["type"]))
                label = f"{src['name']} ({'новости' if kind == 'news' else 'афиша'})"
                if not handler:
                    self.out.errors.append(f"{label}: неизвестный тип {src['type']}")
                    continue
                try:
                    items = handler(src)
                except Exception as exc:
                    log.exception("Источник %s упал", label)
                    self.out.errors.append(f"{label}: {_short_error(exc)}")
                    continue
                self.out.per_source[label] = len(items)
                (self.out.news if kind == "news" else self.out.events).extend(items)
        self.out.news.sort(key=lambda n: n.published or self.now, reverse=True)
        self.out.events.sort(key=lambda e: (e.start or self.horizon_end, e.title))
        return self.out


def collect(fetcher: Fetcher, state: dict, now: datetime, sources: dict,
            news_window_hours: int, event_horizon_days: int) -> Collected:
    return Collector(fetcher, state, now, news_window_hours, event_horizon_days).run(sources)
