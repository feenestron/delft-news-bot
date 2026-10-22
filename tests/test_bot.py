"""Офлайн-тесты: синтетические страницы вместо реальных сайтов, фейковый Telegram."""
from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot import state as state_mod
from bot.collect import Collector
from bot.config import Config
from bot.digest import digest_from_llm, plain_digest, render
from bot.fetch import extract_links, find_dutch_dates, page_details, parse_feed
from bot.moderation import Moderator

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
        html = render(d)
        self.assertIn("<b>🗞 Делфт · четверг, 22 октября</b>", html)
        self.assertIn("📰 События", html)
        self.assertIn("📅 Анонсы", html)
        self.assertIn('<a href="https://www.omroepdelft.nl/delft/brand/">Omroep Delft</a>', html)

    def test_plain_digest_escapes_html(self):
        d = plain_digest(self.collected(), TODAY, 8, 8)
        html = render(d)
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
        self.assertLessEqual(len(render(d)), 4096)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.removed = []
        self.answers = []
        self.updates = []
        self.next_id = 100

    def send(self, chat_id, post, reply_markup=None, reply_to=None):
        self.next_id += 1
        self.sent.append({"chat": chat_id, "post": post, "markup": reply_markup, "id": self.next_id})
        return {"message_id": self.next_id}

    def send_text(self, chat_id, text, reply_to=None):
        return self.send(chat_id, {"text": text})

    def remove_buttons(self, chat_id, message_id):
        self.removed.append(message_id)

    def answer_callback(self, cid, text=""):
        self.answers.append(text)

    def get_updates(self, offset, timeout):
        out = [u for u in self.updates if u["update_id"] >= offset]
        self.updates = []
        return out


ADMIN = 555


def cfg():
    return Config(bot_token="t", admin_chat_id=ADMIN, channel_id="@delft_test", anthropic_key="",
                  model="m", tz=TZ, draft_hour=6, moderation_until_hour=12, news_window_hours=30,
                  event_horizon_days=3, max_news=8, max_events=8, state_path="", sources_path="")


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.st = fresh_state()
        self.tg = FakeTelegram()
        self.saves = 0

        def save():
            self.saves += 1

        self.mod = Moderator(cfg(), self.tg, self.st, save, lambda: NOW)
        self.mod.submit_draft("d1", {"text": "<b>Дайджест</b>", "parse_mode": "HTML"}, "note",
                              ["https://e/1"], ["https://n/1"])
        self.preview = self.st["pending"]["preview_ids"][0]

    def cb(self, uid, data, user=ADMIN):
        return {"update_id": uid, "callback_query": {"id": f"c{uid}", "from": {"id": user}, "data": data}}

    def msg(self, uid, text, reply_to=None, chat=ADMIN, entities=None):
        m = {"message_id": 900 + uid, "chat": {"id": chat}, "text": text}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        if entities:
            m["entities"] = entities
        return {"update_id": uid, "message": m}

    def test_publish_via_button(self):
        self.tg.updates = [self.cb(1, "pub:d1")]
        self.mod.poll()
        channel_posts = [s for s in self.tg.sent if s["chat"] == "@delft_test"]
        self.assertEqual(len(channel_posts), 1)
        self.assertEqual(channel_posts[0]["post"]["parse_mode"], "HTML")
        self.assertEqual(self.st["pending"]["status"], "published")
        self.assertIn("https://e/1", self.st["announced_events"])
        self.assertIn("https://n/1", self.st["seen"])
        self.assertEqual(self.st["tg_offset"], 2)
        self.assertIn(self.preview, self.tg.removed)
        self.assertIn("https://t.me/delft_test/", self.tg.sent[-1]["post"]["text"])

    def test_double_click_publishes_once(self):
        self.tg.updates = [self.cb(1, "pub:d1"), self.cb(2, "pub:d1")]
        self.mod.poll()
        self.assertEqual(len([s for s in self.tg.sent if s["chat"] == "@delft_test"]), 1)

    def test_stranger_cannot_publish(self):
        self.tg.updates = [self.cb(1, "pub:d1", user=999), self.msg(2, "/publish", chat=999)]
        self.mod.poll()
        self.assertEqual(self.st["pending"]["status"], "pending")
        self.assertFalse([s for s in self.tg.sent if s["chat"] == "@delft_test"])

    def test_edit_by_reply_then_publish(self):
        ents = [{"type": "bold", "offset": 0, "length": 4}]
        self.tg.updates = [self.msg(1, "Новый текст", reply_to=self.preview, entities=ents)]
        self.mod.poll()
        p = self.st["pending"]
        self.assertEqual(p["post"], {"text": "Новый текст", "entities": ents})
        self.assertEqual(len(p["preview_ids"]), 2)
        self.tg.updates = [self.cb(2, "pub:d1")]
        self.mod.poll()
        post = [s for s in self.tg.sent if s["chat"] == "@delft_test"][0]["post"]
        self.assertEqual(post["text"], "Новый текст")

    def test_reject_and_stale_draft(self):
        self.tg.updates = [self.cb(1, "rej:d1"), self.cb(2, "pub:old")]
        self.mod.poll()
        self.assertEqual(self.st["pending"]["status"], "rejected")
        self.assertNotIn("https://n/1", self.st["seen"])  # отклонённые новости вернутся завтра
        self.assertIn("Этот черновик устарел", self.tg.answers)
        self.assertFalse([s for s in self.tg.sent if s["chat"] == "@delft_test"])

    def test_new_draft_supersedes_old(self):
        self.mod.submit_draft("d2", {"text": "x", "parse_mode": "HTML"}, "note", [])
        self.assertEqual(self.st["history"][-1]["status"], "superseded")
        self.assertIn(self.preview, self.tg.removed)
        self.tg.updates = [self.cb(1, "pub:d1")]
        self.mod.poll()
        self.assertEqual(self.st["pending"]["status"], "pending")

    def test_added_to_channel_reports_id(self):
        self.tg.updates = [{"update_id": 1, "my_chat_member": {
            "chat": {"id": -1001234, "type": "channel", "title": "Delft test"},
            "new_chat_member": {"status": "administrator"}}}]
        self.mod.poll()
        self.assertIn("-1001234", self.tg.sent[-1]["post"]["text"])

    def test_publish_error_hint(self):
        from bot.telegram import TelegramError

        def boom(chat_id, post, reply_markup=None, reply_to=None):
            if chat_id == "@delft_test":
                raise TelegramError("Forbidden: the bot can't send messages to the bot", 403)
            return FakeTelegram.send(self.tg, chat_id, post, reply_markup, reply_to)

        self.tg.send = boom
        self.tg.updates = [self.cb(1, "pub:d1")]
        self.mod.poll()
        self.assertEqual(self.st["pending"]["status"], "pending")
        self.assertIn("CHANNEL_ID указан бот", self.tg.sent[-1]["post"]["text"])

    def test_setup_mode_reports_chat_id(self):
        c = cfg()
        c.admin_chat_id = None
        mod = Moderator(c, self.tg, fresh_state(), lambda: None, lambda: NOW)
        self.tg.updates = [self.msg(1, "/start", chat=4242)]
        mod.poll()
        self.assertIn("4242", self.tg.sent[-1]["post"]["text"])

    def test_wait_until_returns_after_decision(self):
        clock = [NOW]
        self.mod.now = lambda: clock[0]
        self.tg.updates = [self.cb(1, "pub:d1")]
        self.mod.poll(until=NOW + timedelta(hours=5))  # не должен висеть
        self.assertEqual(self.st["pending"]["status"], "published")


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
