"""Офлайн-тесты: синтетические страницы вместо реальных сайтов, фейковый Telegram."""
from __future__ import annotations

import copy
import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot import state as state_mod
from bot.collect import Collector
from bot.config import Config
from bot.digest import digest_from_llm, plain_digest, render
from bot.fetch import extract_links, find_dutch_dates, page_details, parse_feed

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 10, 22, 6, 25, tzinfo=TZ)
TODAY = NOW.date()

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<title>Omroep Delft</title>
<item><title>Grote brand in binnenstad</title><link>https://www.omroepdelft.nl/delft/brand/</link>
<pubDate>Thu, 22 Oct 2026 05:43:59 +0000</pubDate><description><![CDATA[<p>Tien woningen ontruimd.</p>]]></description></item>
<item><title>Oud bericht</title><link>https://www.omroepdelft.nl/delft/oud/</link>
<pubDate>Mon, 19 Oct 2026 10:00:00 +0000</pubDate><description>oud</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom item</title><link rel="alternate" href="https://example.org/a"/>
<updated>2026-10-22T04:00:00Z</updated><summary>Samenvatting</summary></entry></feed>"""

LISTING = """<html><body><nav><a href="/nieuws/algemeen/1/menu">Menu</a></nav><main>
<a href="/nieuws/algemeen/111395/beter-lopen">Beter lopen begint bij je voeten</a>
<a href="/nieuws/algemeen/111395/beter-lopen#reacties">reacties</a>
<a href="/nieuws/partnerbijdrage/111400/advertentie">Advertentie</a>
<a href="https://elders.nl/x">extern</a>
</main></body></html>"""

ARTICLE = """<html><head>
<meta property="og:title" content="Beter lopen begint bij je voeten">
<meta property="og:description" content="Een artikel over lopen.">
<meta property="article:published_time" content="2026-10-22T05:00:00+02:00">
</head><body><main><p>Tekst</p></main></body></html>"""

EVENT_PAGE = """<html><head><meta property="og:title" content="Science Day TU Delft"></head>
<body><header>Nieuws 3 januari</header><main><h1>Science Day</h1>
<p>Wanneer: Zondag 25 oktober 2026, 11.00 - 17.00 uur</p>
<p>Waar: TU Delft Science Centre</p></main></body></html>"""

EVENT_JSONLD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Event","name":"Concert",
"startDate":"2026-10-24T20:15:00+02:00","location":{"@type":"Place","name":"Oude Kerk"}}]}
</script></head><body><main>Concert</main></body></html>"""


class FakeResponse:
    def __init__(self, body):
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode()

    def json(self):
        import json
        return json.loads(self.text)


class FakeFetcher:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            raise ConnectionError(f"404 {url}")
        return FakeResponse(self.pages[url])


def fresh_state():
    return copy.deepcopy(state_mod.DEFAULT_STATE)


class ParsingTests(unittest.TestCase):
    def test_rss_and_atom(self):
        items = parse_feed(RSS)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["summary"], "Tien woningen ontruimd.")
        self.assertEqual(items[0]["published"].hour, 5)
        atom = parse_feed(ATOM)
        self.assertEqual(atom[0]["link"], "https://example.org/a")

    def test_extract_links_dedup_scope_exclude(self):
        links = extract_links(LISTING, "https://www.delftopzondag.nl/",
                              r"^https://www\.delftopzondag\.nl/nieuws/[a-z0-9-]+/\d+/",
                              r"/nieuws/partnerbijdrage/", "main")
        self.assertEqual([u for u, _, _ in links],
                         ["https://www.delftopzondag.nl/nieuws/algemeen/111395/beter-lopen"])
        self.assertEqual(links[0][1], "Beter lopen begint bij je voeten")

    def test_page_details(self):
        d = page_details(ARTICLE)
        self.assertEqual(d["title"], "Beter lopen begint bij je voeten")
        self.assertEqual(d["published"].isoformat(), "2026-10-22T05:00:00+02:00")
        e = page_details(EVENT_JSONLD)
        self.assertEqual(e["event_start"].date(), date(2026, 10, 24))
        self.assertEqual(e["venue"], "Oude Kerk")

    def test_dutch_dates(self):
        s, e, snip = find_dutch_dates("Wanneer: Zondag 25 oktober 2026, 11.00 - 17.00 uur", TODAY)
        self.assertEqual((s, e), (date(2026, 10, 25), None))
        self.assertIn("11.00 - 17.00", snip)
        s, e, _ = find_dutch_dates("Te zien van 1–31 oktober in de stad", TODAY)
        self.assertEqual((s, e), (date(2026, 10, 1), date(2026, 10, 31)))
        s, e, _ = find_dutch_dates("Expositie 10 okt t/m 1 nov", TODAY)
        self.assertEqual((s, e), (date(2026, 10, 10), date(2026, 11, 1)))
        s, e, _ = find_dutch_dates("Nieuwjaarsduik 1 januari", TODAY)
        self.assertEqual(s, date(2027, 1, 1))
        self.assertEqual(find_dutch_dates("geen datum hier", TODAY)[0], None)

    def test_dates_anchor_far_below(self):
        page = ("<html><body><main><p>Tickets vanaf 7 juli 2026 t/m 26 oktober.</p>"
                + "<p>" + "lorem ipsum " * 400 + "</p>"
                + "<h2>WANNEER</h2><p>Zondag 25 oktober 2026</p></main></body></html>")
        d = page_details(page)
        s, e, _ = find_dutch_dates(d["full_text"], TODAY)
        self.assertEqual((s, e), (date(2026, 10, 25), None))
        self.assertEqual(find_dutch_dates("Op 5 november", TODAY, anchored_only=True)[0], None)

    def test_text_dates_beat_jsonld_listing_period(self):
        listing = '<main><a href="/nl/uitagenda/uitagenda-delft/1/sd">SD</a></main>'
        page = ('<html><head><script type="application/ld+json">{"@type":"Event",'
                '"startDate":"2025-07-07","endDate":"2026-10-26"}</script></head>'
                '<body><main><p>Wanneer: zondag 25 oktober 2026</p></main></body></html>')
        src = {"name": "In Delft", "url": "https://www.indelft.nl/nl/uitagenda",
               "link_pattern": r"^https://www\.indelft\.nl/nl/uitagenda/uitagenda-delft/\d+/"}
        c = Collector(FakeFetcher({src["url"]: listing,
                                   "https://www.indelft.nl/nl/uitagenda/uitagenda-delft/1/sd": page}),
                      fresh_state(), NOW, 30, 3)
        items = c.events_html(src)
        self.assertEqual((items[0].start, items[0].end), (date(2026, 10, 25), None))


class CollectTests(unittest.TestCase):
    def make(self, pages, state=None):
        st = state or fresh_state()
        return Collector(FakeFetcher(pages), st, NOW, news_window_hours=30, event_horizon_days=3), st

    def test_rss_window_and_seen(self):
        c, st = self.make({"https://feed": RSS})
        items = c.news_rss({"name": "Omroep Delft", "url": "https://feed"})
        self.assertEqual([i.title for i in items], ["Grote brand in binnenstad"])
        self.assertEqual(len(c.out.seen_urls), 2)
        st["seen"]["https://www.omroepdelft.nl/delft/brand/"] = "x"
        c2, _ = self.make({"https://feed": RSS}, st)
        self.assertEqual(c2.news_rss({"name": "O", "url": "https://feed"}), [])

    def test_html_news_bootstrap(self):
        url = "https://www.delftopzondag.nl/nieuws/algemeen/111395/beter-lopen"
        undated = ARTICLE.replace('<meta property="article:published_time" content="2026-10-22T05:00:00+02:00">', "")
        src = {"name": "DoZ", "url": "https://www.delftopzondag.nl/",
               "link_pattern": r"^https://www\.delftopzondag\.nl/nieuws/[a-z0-9-]+/\d+/",
               "exclude_pattern": "/nieuws/partnerbijdrage/", "scope_selector": "main"}
        # первый запуск: без даты — пропускаем, но помечаем увиденным
        c, st = self.make({src["url"]: LISTING, url: undated})
        self.assertEqual(c.news_html(src), [])
        self.assertEqual(c.out.seen_urls, [url])
        self.assertEqual(c.out.newly_bootstrapped, ["DoZ"])
        # после bootstrap новая ссылка без даты считается свежей
        st2 = fresh_state()
        st2["bootstrapped_sources"].append("DoZ")
        c2, _ = self.make({src["url"]: LISTING, url: undated}, st2)
        self.assertEqual(len(c2.news_html(src)), 1)

    def test_events_from_url_dates(self):
        listing = """<main>
        <a href="/agenda/2026/10/23/concert-1">Concert vrijdag</a>
        <a href="/agenda/2026/11/30/later-2">Te laat</a>
        <a href="/agenda/2026/10/21/gisteren-3">Gisteren</a></main>"""
        src = {"name": "DoZ agenda", "url": "https://www.delftopzondag.nl/agenda",
               "link_pattern": r"^https://www\.delftopzondag\.nl/agenda/(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})/"}
        c, _ = self.make({src["url"]: listing})  # страницы событий недоступны — берём текст ссылки
        items = c.events_html(src)
        self.assertEqual([(i.title, i.start) for i in items], [("Concert vrijdag", date(2026, 10, 23))])

    def test_events_dates_from_page_and_cache(self):
        listing = '<main><a href="/nl/uitagenda/uitagenda-delft/1/science-day">Science Day</a></main>'
        page_url = "https://www.indelft.nl/nl/uitagenda/uitagenda-delft/1/science-day"
        src = {"name": "In Delft", "url": "https://www.indelft.nl/nl/uitagenda",
               "link_pattern": r"^https://www\.indelft\.nl/nl/uitagenda/uitagenda-delft/\d+/"}
        c, st = self.make({src["url"]: listing, page_url: EVENT_PAGE})
        items = c.events_html(src)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].start, date(2026, 10, 25))
        self.assertIn(page_url, st["event_cache"])
        # второй запуск — страница не перекачивается
        c2, _ = self.make({src["url"]: listing}, st)
        self.assertEqual(len(c2.events_html(src)), 1)
        self.assertEqual(c2.f.calls, [src["url"]])

    def test_announced_events_skipped(self):
        listing = '<main><a href="/agenda/2026/10/23/concert-1">Concert</a></main>'
        src = {"name": "A", "url": "https://www.delftopzondag.nl/agenda",
               "link_pattern": r"^https://www\.delftopzondag\.nl/agenda/(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})/"}
        st = fresh_state()
        st["announced_events"]["https://www.delftopzondag.nl/agenda/2026/10/23/concert-1"] = NOW.isoformat()
        c, _ = self.make({src["url"]: listing}, st)
        self.assertEqual(c.events_html(src), [])

    def test_tribe_events(self):
        api = ('{"events":[{"title":"Tim Burton&#8217;s Universe","url":"https://uitdelft.com/event/tb/",'
               '"start_date":"2026-10-24 20:00:00","end_date":"2026-10-24 22:00:00","all_day":false,'
               '"cost":"€25","venue":{"venue":"Nieuwe Kerk","address":"Markt 80","city":"Delft"}},'
               '{"title":"Den Haag","url":"https://uitdelft.com/event/dh/","start_date":"2026-10-23 10:00:00",'
               '"venue":{"venue":"X","city":"Den Haag"}}]}')
        c, _ = self.make({})
        c.f.get = lambda url, **kw: FakeResponse(api)
        items = c.events_tribe({"name": "Uit Delft", "url": "https://uitdelft.com", "cities": ["Delft"]})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Tim Burton’s Universe")
        self.assertEqual(items[0].time_text, "20:00–22:00")
        self.assertEqual(items[0].venue, "Nieuwe Kerk, Markt 80")

    def test_run_collects_errors(self):
        c, _ = self.make({"https://feed": RSS})
        out = c.run({"news": [{"name": "Omroep Delft", "type": "rss", "url": "https://feed"},
                              {"name": "Kapot", "type": "rss", "url": "https://broken"}],
                     "events": []})
        self.assertEqual(len(out.news), 1)
        self.assertEqual(len(out.errors), 1)
        self.assertIn("Kapot", out.errors[0])


class DigestTests(unittest.TestCase):
    def collected(self):
        c, _ = CollectTests().make({"https://feed": RSS})
        c.run({"news": [{"name": "Omroep Delft", "type": "rss", "url": "https://feed"}], "events": []})
        from bot.collect import Event
        c.out.events.append(Event("Uit Delft", "Concert <live>", "https://e/1", date(2026, 10, 24),
                                  None, "20:15", "Oude Kerk", "€10"))
        return c.out

    def test_llm_result_uses_only_known_ids(self):
        col = self.collected()
        result = {"news": [{"id": "n0", "title": "Крупный пожар в центре", "summary": "Эвакуированы 10 домов."},
                           {"id": "n99", "title": "выдумка", "summary": ""}],
                  "events": [{"id": "e0", "title": "Концерт", "when": "сб 24 окт, 20:15",
                              "date": "2026-10-24", "where": "Oude Kerk", "price": "€10"}]}
        d = digest_from_llm(result, col, TODAY, 8, 8)
        self.assertEqual(len(d.news), 1)
        self.assertEqual(d.news[0].url, "https://www.omroepdelft.nl/delft/brand/")
        html = "\n".join(render(d))
        self.assertIn("<b>🗞 Делфт · четверг, 22 октября</b>", html)
        self.assertIn('<b>Крупный пожар в центре</b> — <a href="https://www.omroepdelft.nl/delft/brand/">'
                      'Omroep Delft</a>\n<i>Эвакуированы 10 домов.</i>', html)
        self.assertIn('сб 24 окт, 20:15 — <a href="https://e/1">Концерт</a>\n<i>Oude Kerk · €10</i>', html)
        self.assertIn("📰 События", html)
        self.assertIn("📅 Анонсы", html)
        self.assertIn('<a href="https://www.omroepdelft.nl/delft/brand/">Omroep Delft</a>', html)

    def test_events_sorted_by_time_within_day(self):
        col = self.collected()
        from bot.collect import Event
        col.events.append(Event("U", "Early", "https://e/2", date(2026, 10, 24)))
        result = {"news": [], "events": [
            {"id": "e0", "title": "B", "when": "сб 24 окт, 20:15", "date": "2026-10-24"},
            {"id": "e1", "title": "A", "when": "сб 24 окт, 9:30", "date": "2026-10-24"}]}
        d = digest_from_llm(result, col, TODAY, 8, 8)
        self.assertEqual([e.title for e in d.events], ["A", "B"])

    def test_plain_digest_escapes_html(self):
        d = plain_digest(self.collected(), TODAY, 8, 8)
        html = "\n".join(render(d))
        self.assertIn("Concert &lt;live&gt;", html)
        self.assertIn("сб 24 окт, 20:15", html)

    def test_claude_falls_back_to_auto_tool_choice(self):
        from unittest import mock
        from bot import digest as dg

        class R:
            def __init__(self, code, data):
                self.status_code, self._data = code, data
                self.text = __import__("json").dumps(data)

            def json(self):
                return self._data

        err = R(400, {"type": "error", "error": {"type": "invalid_request_error", "message":
                      'tool_choice: type "tool" and "any" are not supported with thinking'}})
        ok = R(200, {"content": [{"type": "thinking", "thinking": "..."},
                                 {"type": "tool_use", "name": "submit_digest",
                                  "input": {"news": [{"id": "n0", "title": "Пожар", "summary": "x"}],
                                            "events": []}}]})
        calls = []

        def fake_post(url, json=None, **kw):
            calls.append(json["tool_choice"]["type"])
            return err if len(calls) == 1 else ok

        with mock.patch.object(dg.requests, "post", side_effect=fake_post):
            d = dg.llm_digest(self.collected(), NOW, TODAY + timedelta(days=3), "k", "m", 8, 8)
        self.assertEqual(calls, ["tool", "auto"])
        self.assertEqual(d.news[0].title, "Пожар")

    def test_extract_result_from_text_json(self):
        from bot.digest import extract_result
        content = [{"type": "text", "text": 'Вот дайджест:\n```json\n{"news": [], "events": []}\n```'}]
        self.assertEqual(extract_result(content), {"news": [], "events": []})
        self.assertIsNone(extract_result([{"type": "text", "text": "нет данных"}]))

    def test_render_limit(self):
        from bot.digest import Digest, NewsLine
        d = Digest(TODAY, [NewsLine("Заголовок " * 10, "Текст " * 60, "https://x/" + str(i), "Src")
                           for i in range(30)], [])
        messages = render(d)
        self.assertGreater(len(messages), 1)  # длинный дайджест делится, пункты не теряются
        self.assertTrue(all(len(m) <= 4096 for m in messages))
        self.assertEqual(sum(m.count("https://x/") for m in messages), 30)


class FakeTelegram:
    def __init__(self, fail_for=None):
        self.sent = []
        self.updates = []
        self.next_id = 100
        self.fail_for = fail_for or {}

    def send(self, chat_id, post, reply_markup=None, reply_to=None):
        if chat_id in self.fail_for:
            raise self.fail_for[chat_id]
        self.next_id += 1
        self.sent.append({"chat": chat_id, "post": post, "id": self.next_id})
        return {"message_id": self.next_id}

    def send_text(self, chat_id, text, reply_to=None):
        return self.send(chat_id, {"text": text})

    def call(self, method, **kw):
        return {"username": "delft_bot"}

    def get_updates(self, offset, timeout):
        out = [u for u in self.updates if u["update_id"] >= offset]
        self.updates = []
        return out


ADMIN = 555


def cfg(path=""):
    return Config(bot_token="t", admin_chat_id=ADMIN, channel_id="@delft_test", anthropic_key="",
                  model="m", tz=TZ, post_hour=6, post_until_hour=12, news_window_hours=30,
                  event_horizon_days=3, max_news=8, max_events=8, state_path=path, sources_path="")


class PostingTests(unittest.TestCase):
    """Публикация без модерации: сразу в группу, админу — только при проблемах."""

    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        from bot import __main__ as main_mod
        from bot.collect import Event

        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")
        self.main = main_mod
        self.tg = FakeTelegram()
        c, _ = CollectTests().make({"https://feed": RSS})
        c.run({"news": [{"name": "Omroep Delft", "type": "rss", "url": "https://feed"}], "events": []})
        c.out.events.append(Event("Uit Delft", "Concert", "https://e/1", date(2026, 10, 24)))
        self.collected = c.out
        self.patches = [
            mock.patch.object(main_mod, "Telegram", lambda token: self.tg),
            mock.patch.object(main_mod, "collect", lambda *a, **k: self.collected),
            mock.patch.object(main_mod, "load_sources", lambda p: {}),
            mock.patch.object(main_mod, "Fetcher", lambda: None),
        ]
        for p in self.patches:
            p.start()
        self.now = NOW

    def tearDown(self):
        from unittest import mock
        mock.patch.stopall()
        self.tmp.cleanup()

    def run_digest(self, force=False, conf=None):
        import argparse
        from unittest import mock

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return self.now

        with mock.patch.object(self.main, "datetime", Clock):
            return self.main.cmd_digest(conf or cfg(self.path),
                                        argparse.Namespace(force=force, dry_run=False))

    def test_posts_directly_and_marks_seen(self):
        self.run_digest()
        posts = [s for s in self.tg.sent if s["chat"] == "@delft_test"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["post"]["parse_mode"], "HTML")
        st = state_mod.load(self.path)
        self.assertEqual(st["last_post_date"], "2026-10-22")
        self.assertIn("https://e/1", st["announced_events"])
        self.assertIn("https://www.omroepdelft.nl/delft/brand/", st["seen"])
        # без ANTHROPIC_API_KEY это «проблема» — админ получает заметку
        self.assertTrue(any(s["chat"] == ADMIN for s in self.tg.sent))

    def test_published_digest_saved_as_json(self):
        import json as _json
        self.run_digest()
        base = os.path.dirname(self.path)
        with open(os.path.join(base, "latest.json"), encoding="utf-8") as f:
            latest = _json.load(f)
        self.assertEqual(latest["date"], "2026-10-22")
        self.assertTrue(latest["news"] and latest["events"])
        self.assertTrue(all(n["url"].startswith("http") for n in latest["news"]))
        self.assertTrue(os.path.exists(os.path.join(base, "digests", "2026-10-22.json")))
        with open(os.path.join(base, "digests", "index.json"), encoding="utf-8") as f:
            self.assertEqual(_json.load(f), ["2026-10-22"])

    def test_archive_keeps_last_days(self):
        from datetime import date as _date
        from bot.digest import Digest
        base = os.path.dirname(self.path)
        for day in range(1, 11):
            self.main.save_archive(self.path, Digest(_date(2026, 10, day), [], []), self.now)
        names = sorted(os.listdir(os.path.join(base, "digests")))
        self.assertEqual(len([n for n in names if n != "index.json"]), self.main.ARCHIVE_DAYS)
        self.assertNotIn("2026-10-03.json", names)
        self.assertIn("2026-10-10.json", names)

    def test_nothing_published_keeps_previous_archive(self):
        self.collected.news.clear()
        self.collected.events.clear()
        self.run_digest()
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.path), "latest.json")))

    def test_once_per_day_and_time_window(self):
        self.run_digest()
        self.run_digest()  # второй запуск того же дня ничего не публикует
        self.assertEqual(len([s for s in self.tg.sent if s["chat"] == "@delft_test"]), 1)
        self.now = NOW.replace(day=23, hour=13)  # после окна
        self.run_digest()
        self.now = NOW.replace(day=23, hour=5)   # до окна
        self.run_digest()
        self.assertEqual(len([s for s in self.tg.sent if s["chat"] == "@delft_test"]), 1)
        self.now = NOW.replace(day=23, hour=9)   # расписание опоздало — всё равно публикуем
        self.run_digest()
        self.assertEqual(len([s for s in self.tg.sent if s["chat"] == "@delft_test"]), 2)

    def test_legacy_state_counts_as_posted_today(self):
        state_mod.save(self.path, {"last_draft_date": "2026-10-22", "pending": {"id": "x"}})
        self.run_digest()
        self.assertFalse([s for s in self.tg.sent if s["chat"] == "@delft_test"])
        self.assertNotIn("pending", state_mod.load(self.path))

    def test_nothing_to_post_notifies_admin_only(self):
        self.collected.news.clear()
        self.collected.events.clear()
        self.run_digest()
        self.assertFalse([s for s in self.tg.sent if s["chat"] == "@delft_test"])
        self.assertIn("новых материалов нет", self.tg.sent[-1]["post"]["text"])
        self.assertEqual(state_mod.load(self.path)["last_post_date"], "2026-10-22")

    def test_failed_post_keeps_state_untouched(self):
        from bot.telegram import TelegramError
        self.tg.fail_for = {"@delft_test": TelegramError("Forbidden: bot was kicked", 403)}
        with self.assertRaises(TelegramError):
            self.run_digest()
        st = state_mod.load(self.path)
        self.assertIsNone(st["last_post_date"])
        self.assertEqual(st["seen"], {})

    def test_group_migrated_to_supergroup(self):
        from bot.telegram import TelegramError
        self.tg.fail_for = {"@delft_test": TelegramError("group upgraded", 400,
                                                         {"migrate_to_chat_id": -100777})}
        self.run_digest()
        self.assertEqual([s["chat"] for s in self.tg.sent if s["chat"] != ADMIN], ["-100777"])
        self.assertIn("-100777", self.tg.sent[-1]["post"]["text"])  # заметка админу

    def test_chats_lists_groups(self):
        self.tg.updates = [{"update_id": 7, "my_chat_member": {
            "chat": {"id": -1009, "type": "supergroup", "title": "Delft"},
            "new_chat_member": {"status": "administrator"}}}]
        self.main.cmd_chats(cfg(self.path), None)
        st = state_mod.load(self.path)
        self.assertIn("-1009", st["known_chats"])
        self.assertEqual(st["tg_offset"], 8)

    def test_chats_marks_current_channel(self):
        import os
        summary = os.path.join(self.tmp.name, "summary.md")
        os.environ["GITHUB_STEP_SUMMARY"] = summary
        try:
            state_mod.save(self.path, {"known_chats": {"-1009": "Delft (supergroup)"}})
            c = cfg(self.path)
            c.channel_id = "-1009"
            self.main.cmd_chats(c, None)
            text = open(summary, encoding="utf-8").read()
        finally:
            del os.environ["GITHUB_STEP_SUMMARY"]
        self.assertIn("уже указана в `CHANNEL_ID`", text)
        self.assertNotIn("не совпадает", text)


class StateTests(unittest.TestCase):
    def test_roundtrip_and_prune(self):
        import os
        import tempfile
        st = fresh_state()
        st["seen"] = {"old": (NOW - timedelta(days=100)).isoformat(), "new": NOW.isoformat()}
        state_mod.prune(st, NOW)
        self.assertEqual(list(st["seen"]), ["new"])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state", "s.json")
            state_mod.save(p, st)
            self.assertEqual(state_mod.load(p)["seen"], {"new": NOW.isoformat()})


if __name__ == "__main__":
    unittest.main()
