"""HTTP-загрузка с уважением к robots.txt и извлечение данных со страниц."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

log = logging.getLogger(__name__)

USER_AGENT = ("DelftNewsDigestBot/1.0 (+https://github.com/feenestron/delft-news-bot; "
              "daily digest, 1 visit per day)")


class Fetcher:
    def __init__(self, timeout: int = 20, per_host_delay: float = 1.0):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl,en;q=0.8",
        })
        self.timeout = timeout
        self.per_host_delay = per_host_delay
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(host)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(host + "/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[host] = rp
        return rp.can_fetch(USER_AGENT, url)

    def get(self, url: str, **kwargs) -> requests.Response:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt запрещает {url}")
        host = urlparse(url).netloc
        wait = self.per_host_delay - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()
        r = self.session.get(url, timeout=self.timeout, **kwargs)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------- HTML helpers

def clean_text(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    if "<" in text or "&" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def extract_links(html: str, base_url: str, pattern: str, exclude: str | None = None,
                  scope_selector: str | None = None) -> list[tuple[str, str, re.Match]]:
    """Ссылки страницы, подходящие под pattern: [(url, текст ссылки, match)] без дублей."""
    soup = BeautifulSoup(html, "html.parser")
    scope = (soup.select_one(scope_selector) if scope_selector else None) or soup
    rx = re.compile(pattern)
    rx_ex = re.compile(exclude) if exclude else None
    found: dict[str, tuple[str, str, re.Match]] = {}
    for a in scope.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"].strip()))[0]
        m = rx.search(url)
        if not m or (rx_ex and rx_ex.search(url)):
            continue
        text = clean_text(a.get_text(" "))
        if url not in found:
            found[url] = (url, text, m)
        elif len(text) > len(found[url][1]):  # карточки часто содержат несколько ссылок
            found[url] = (url, text, m)
    return list(found.values())


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return dtparser.isoparse(str(value).strip())
    except (ValueError, TypeError):
        try:
            return dtparser.parse(str(value).strip())
        except (ValueError, TypeError, OverflowError):
            return None


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def page_details(html: str, scope_selector: str | None = None) -> dict:
    """Заголовок, описание, дата публикации, данные события и текст основной части страницы."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names: str) -> str:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = meta("og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ") if h1 else (soup.title.string if soup.title else "")
    details = {
        "title": clean_text(title),
        "description": clean_text(meta("og:description", "description", "twitter:description"), 600),
        "published": None,
        "event_start": None,
        "event_end": None,
        "venue": "",
    }
    published = _parse_dt(meta("article:published_time", "og:article:published_time",
                               "datePublished", "dcterms.date"))
    for node in _iter_jsonld(soup):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if not published and node.get("datePublished"):
            published = _parse_dt(node["datePublished"])
        if any(t and "Event" in str(t) for t in types) and node.get("startDate"):
            details["event_start"] = _parse_dt(node.get("startDate"))
            details["event_end"] = _parse_dt(node.get("endDate"))
            loc = node.get("location")
            if isinstance(loc, list):
                loc = loc[0] if loc else None
            if isinstance(loc, dict):
                details["venue"] = clean_text(loc.get("name", ""))
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = _parse_dt(t["datetime"]) if t else None
    details["published"] = published

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    scope = (soup.select_one(scope_selector) if scope_selector else None) \
        or soup.select_one("main") or soup.select_one("article") or soup.body or soup
    full = clean_text(scope.get_text(" "))
    details["text"] = clean_text(full, 2500)
    details["full_text"] = full[:30000]  # для поиска дат: блок «Wanneer» бывает далеко внизу
    return details


# ------------------------------------------------------------------ RSS/Atom

def parse_feed(content: bytes) -> list[dict]:
    """RSS 2.0 / Atom → [{"title", "link", "summary", "published"}]."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    root = ET.fromstring(content)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def child_text(node, *names: str) -> str:
        for c in node:
            if local(c.tag) in names and (c.text or "").strip():
                return c.text.strip()
        return ""

    def when(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return _parse_dt(value)

    items = []
    for node in root.iter():
        kind = local(node.tag)
        if kind not in ("item", "entry"):
            continue
        link = child_text(node, "link")
        if not link:  # Atom: <link href="..." rel="alternate"/>
            for c in node:
                if local(c.tag) == "link" and c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
        items.append({
            "title": clean_text(child_text(node, "title")),
            "link": link.strip(),
            "summary": clean_text(child_text(node, "description", "summary", "encoded", "content"), 600),
            "published": when(child_text(node, "pubDate", "published", "updated", "date")),
        })
    return items


# ------------------------------------------------------ нидерландские даты

MONTHS = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maart": 3, "mrt": 3, "mar": 3,
    "april": 4, "apr": 4, "mei": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "augustus": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE_RX = re.compile(rf"\b(\d{{1,2}})(?:\s*[-–]\s*(\d{{1,2}}))?\s+({_MON})\b\.?(?:\s+(\d{{4}}))?",
                      re.IGNORECASE)
_RANGE_RX = re.compile(r"^\s*(?:t/?m|tot(?:\s+en\s+met)?|[-–]|until|to)\s*$", re.IGNORECASE)
_TIME_RX = re.compile(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)(?:\s*(?:uur)?\s*(?:[-–]|tot)\s*([01]?\d|2[0-3])[.:]([0-5]\d))?")


def _mkdate(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        if year:
            return date(year, month, day)
        d = date(today.year, month, day)
        if d < today - timedelta(days=120):  # "5 jan" в октябре — это следующий год
            d = date(today.year + 1, month, day)
        return d
    except ValueError:
        return None


def find_dutch_dates(text: str, today: date) -> tuple[date | None, date | None, str]:
    """Первая дата (или диапазон) в тексте: (начало, конец, фрагмент текста вокруг)."""
    if not text:
        return None, None, ""
    anchor = re.search(r"\b(Wanneer|Datum|Data|When)\b", text, re.IGNORECASE)
    search_from = anchor.start() if anchor else 0
    matches = list(_DATE_RX.finditer(text, search_from)) or list(_DATE_RX.finditer(text))
    if not matches:
        return None, None, ""
    m = matches[0]
    month = MONTHS[m.group(3).lower()]
    year = int(m.group(4)) if m.group(4) else None
    if m.group(2):  # "1–31 oktober"
        start = _mkdate(int(m.group(1)), month, year, today)
        end = _mkdate(int(m.group(2)), month, year, today)
        span_end = m.end()
    else:
        start = _mkdate(int(m.group(1)), month, year, today)
        end, span_end = None, m.end()
        if len(matches) > 1:
            between = text[m.end():matches[1].start()]
            if len(between) <= 12 and _RANGE_RX.match(between):
                n = matches[1]
                end = _mkdate(int(n.group(1)), MONTHS[n.group(3).lower()],
                              int(n.group(4)) if n.group(4) else None, today)
                span_end = n.end()
                if start and end and not year and start > end:  # "20 dec t/m 5 jan"
                    start = start.replace(year=start.year - 1)
    snippet = text[max(0, m.start() - 40): span_end + 80]
    tm = _TIME_RX.search(text, m.start(), m.start() + 300)
    if tm and tm.group(0) not in snippet:
        snippet += " · " + tm.group(0)
    return start, end, clean_text(snippet)
