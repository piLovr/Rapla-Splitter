"""Tests gegen einen echten Rapla-Export.

Aufruf:  RAPLA_ICS=/pfad/zur/25B6.ics python3 test_proxy.py
"""
import os
import re
import threading
import time
import unittest
import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app

REAL_FETCH = app.fetch  # Http-Tests ersetzen app.fetch, die Cache-/Schutz-Tests brauchen das Original
ICS_PATH = os.environ.get("RAPLA_ICS", "25B6.ics")
CFG = app.load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
with open(ICS_PATH, encoding="utf-8") as f:
    TEXT = f.read()


def summaries(feed):
    return [app.parse_event(b)["summary"] for k, b in app.parse_items(feed) if k == "event"]


def feed(cal, wahl="", ccna=False, nach=""):
    enabled = {"ccna"} if ccna else set()
    return app.build_feed(TEXT, cal, "25B6", wahl.split(",") if wahl else [], enabled, CFG, nach.split(",") if nach else [])


class Filtering(unittest.TestCase):
    def events_per_calendar(self, **kw):
        return {cal: ["".join(app.dedupe_dtstamp(b)) for k, b in app.parse_items(feed(cal, **kw)) if k == "event"] for cal in app.CALENDARS}

    def test_vier_kalender_ueberschneidungsfrei_und_vollstaendig(self):
        original = sorted("".join(app.dedupe_dtstamp(b)) for k, b in app.parse_items(TEXT) if k == "event")
        for kw in ({}, {"wahl": "Signale"}, {"wahl": "Linux", "ccna": True}, {"wahl": "Signale", "nach": "geladene"}, {"nach": "alle"}):
            merged = sorted(e for v in self.events_per_calendar(**kw).values() for e in v)
            self.assertEqual(merged, original, kw)

    def test_veranstaltungen_enthaelt_infoveranstaltung(self):
        s = " ".join(summaries(feed("veranstaltungen")))
        for expected in ("Semesterstart-Besprechung", "Programmieren - Übungstermin", "Infoveranstaltung"):
            self.assertIn(expected, s)

    def test_feiertage_nur_im_rest(self):
        holidays = ("Karfreitag", "Ostermontag", "Fschingsdienstag", "Klausurwoche", "Beginn Theorie")
        for cal in ("vorlesungen", "veranstaltungen", "klausuren"):
            blob = " ".join(summaries(feed(cal, ccna=True, nach="alle")))
            for bad in holidays:
                self.assertNotIn(bad, blob, cal)
        rest = " ".join(summaries(feed("rest")))
        for holiday in holidays:
            self.assertIn(holiday, rest)

    def test_abgewaehltes_landet_im_rest(self):
        rest = "\n".join(summaries(feed("rest", wahl="Signale")))
        for other in ("Digitale Währungssysteme", "Geschäftsprozesse", "Webengineering 3", "Linux Basics", "Klausur Linux Basics",
                      "Klausur Geschäftsprozesse", "Klausur Digitale Währungssysteme", "CCNA"):
            self.assertIn(other, rest)
        for mine in ("Signale und Systeme", "Klausur Signale und Systeme", "Netztechnik"):
            self.assertNotIn(mine, rest)
        self.assertIn("CCNA", "\n".join(summaries(feed("vorlesungen", wahl="Signale", ccna=True))))
        self.assertNotIn("CCNA", "\n".join(summaries(feed("rest", wahl="Signale", ccna=True))))

    def test_nachklausur_nur_auf_wunsch_in_klausuren(self):
        self.assertNotIn("geladene", "\n".join(summaries(feed("klausuren", wahl="Linux"))))
        self.assertIn("nur geladene", "\n".join(summaries(feed("rest", wahl="Linux"))))
        for nach in ("geladene", "TINF25", "alle", "  NUR Geladene "):
            self.assertTrue(any("nur geladene" in x for x in summaries(feed("klausuren", wahl="Linux", nach=nach))), nach)
            self.assertFalse(any("nur geladene" in x for x in summaries(feed("rest", wahl="Linux", nach=nach))), nach)
        self.assertFalse(any("geladene" in x for x in summaries(feed("klausuren", wahl="Linux", nach="Quanten"))))

    def test_tippfehler_im_titel(self):
        for cal in ("vorlesungen", "klausuren"):
            self.assertNotIn("Webengineeering", "\n".join(summaries(feed(cal, wahl="Signale"))), cal)

    def test_vorlesungen_wahlfach_signale(self):
        blob = "\n".join(summaries(feed("vorlesungen", wahl="Signale")))
        self.assertIn("Signale und Systeme", blob)
        for other in ("Digitale Währungssysteme", "Geschäftsprozesse", "Webengineering 3", "Linux Basics"):
            self.assertNotIn(other, blob)
        for pflicht in ("Netztechnik", "Statistik", "Rechnerarchitecturen", "Software Engineering", "Formale Sprachen", "Database"):
            self.assertIn(pflicht, blob)

    def test_ccna_nur_mit_flag(self):
        self.assertNotIn("CCNA", "\n".join(summaries(feed("vorlesungen", wahl="Linux"))))
        self.assertIn("CCNA", "\n".join(summaries(feed("vorlesungen", wahl="Linux", ccna=True))))

    def test_klausuren_wahlfach_linux(self):
        s = "\n".join(summaries(feed("klausuren", wahl="Linux")))
        self.assertIn("Klausur Linux Basics", s)
        for pflicht in ("Klausur Statistik", "Klausur Netztechnik", "Klausur Formale Sprachen", "Klausur Rechnerarchitekturen", "Prüfung Programmieren"):
            self.assertIn(pflicht, s)
        for other in ("Klausur Geschäftsprozesse", "Klausur Digitale Währungssysteme", "Klausur Signale und Systeme"):
            self.assertNotIn(other, s)
        self.assertNotIn("geladene", s)

    def test_ohne_wahl_alles_behalten(self):
        blob = "\n".join(summaries(feed("vorlesungen")))
        for f in ("Signale und Systeme", "Digitale Währungssysteme", "Geschäftsprozesse", "Webengineering 3", "Linux Basics"):
            self.assertIn(f, blob)

    def test_stichwort_schreibweisen(self):
        for kw, expect in (("Währung", "Digitale Währungssysteme"), ("wahrung", "Digitale Währungssysteme"),
                           ("Web Engineering", "Webengineering 3"), ("GESCHÄFT", "Geschäftsprozesse"), ("Linux", "Linux Basics")):
            self.assertEqual(app.resolve_wahl([kw], CFG["wahlfach_pool"]), {expect}, kw)

    def test_falsches_stichwort(self):
        with self.assertRaises(ValueError):
            app.resolve_wahl(["Quantenphysik"], CFG["wahlfach_pool"])
        with self.assertRaises(ValueError):  # trifft "Signale und Systeme" UND "...Währungssysteme"
            app.resolve_wahl(["Systeme"], CFG["wahlfach_pool"])

    def test_dtstamp_nur_einmal_pro_termin(self):
        out = feed("vorlesungen", wahl="Signale")
        for k, b in app.parse_items(out):
            if k == "event":
                self.assertEqual(sum(1 for g in app.logical_lines(b) if app.prop_name(g) == "DTSTAMP"), 1)
        self.assertGreater(TEXT.count("DTSTAMP"), out.count("DTSTAMP"))

    def test_serientermine_bleiben_erhalten(self):
        out = feed("vorlesungen", wahl="Signale")
        self.assertIn("RRULE:FREQ=WEEKLY", out)
        self.assertIn("EXDATE;TZID=Europe/Berlin", out)
        # Ausnahmetermin (Statistik am 1.10. in anderem Raum) bleibt neben der Serie
        self.assertEqual(sum(1 for s in summaries(out) if s.startswith("Statistik")), 2)

    def test_kalenderstruktur_gueltig(self):
        out = feed("klausuren", wahl="Linux")
        self.assertTrue(out.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(out.rstrip().endswith("END:VCALENDAR"))
        self.assertIn("BEGIN:VTIMEZONE", out)
        self.assertEqual(out.count("BEGIN:VEVENT"), out.count("END:VEVENT"))
        self.assertIn("X-WR-CALNAME:25B6 Klausuren", out)

    def test_original_zeilen_unveraendert(self):
        out = feed("vorlesungen", wahl="Signale")
        src_lines = set(TEXT.splitlines())
        extra = [l for l in out.splitlines() if l not in src_lines]
        self.assertEqual(sorted(extra), ["X-PUBLISHED-TTL:PT1H", "X-WR-CALNAME:25B6 Vorlesungen"])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.CFG = CFG
        app.fetch = lambda user, file: TEXT
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read().decode()

    base = "user=li%40dhbw-karlsruhe.aa&file=25B6"

    def test_ok(self):
        code, ctype, body = self.get(f"/vorlesungen.ics?{self.base}&wahl=Signale")
        self.assertEqual(code, 200)
        self.assertIn("text/calendar", ctype)
        self.assertIn("Signale und Systeme", body)
        self.assertNotIn("Linux Basics", body)

    def test_ccna_flag(self):
        self.assertNotIn("CCNA", self.get(f"/vorlesungen.ics?{self.base}")[2])
        self.assertIn("CCNA", self.get(f"/vorlesungen.ics?{self.base}&ccna=1")[2])

    def test_ungueltige_parameter(self):
        self.assertEqual(self.get("/vorlesungen.ics?file=25B6")[0], 400)
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=../../etc")[0], 400)
        code, _, body = self.get(f"/vorlesungen.ics?{self.base}&wahl=Quanten")
        self.assertEqual(code, 400)
        self.assertIn("Signale und Systeme", body)

    def test_routen(self):
        self.assertEqual(self.get("/health")[0], 200)
        self.assertEqual(self.get("/foo.ics")[0], 404)
        self.assertEqual(self.get(f"/nachklausuren.ics?{self.base}")[0], 404)  # Kalender wurde entfernt
        for cal in app.CALENDARS:
            self.assertEqual(self.get(f"/{cal}.ics?{self.base}")[0], 200, cal)

    def test_alter_semester_pfad(self):
        old = self.get(f"/semester.ics?{self.base}")
        new = self.get(f"/veranstaltungen.ics?{self.base}")
        self.assertEqual(old[0], 200)
        self.assertIn("Infoveranstaltung", old[2])
        self.assertEqual(sorted(re.findall(r"SUMMARY:.*", old[2])), sorted(re.findall(r"SUMMARY:.*", new[2])))

    def test_url_optionen_fuer_alle_kalender(self):
        for cal in app.CALENDARS:
            self.assertEqual(self.get(f"/{cal}.ics?{self.base}&wahl=Signale&ccna=1&nach=geladene")[0], 200, cal)
            self.assertEqual(self.get(f"/{cal}.ics?{self.base}&wahl=Quanten")[0], 400, cal)
        rest_plain = self.get(f"/rest.ics?{self.base}")[2]
        rest_signale = self.get(f"/rest.ics?{self.base}&wahl=Signale")[2]
        self.assertNotEqual(rest_plain, rest_signale)

    def test_ohne_token_und_mit_beliebigem_token(self):
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6")[0], 200)
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6&token=altes-token")[0], 200)

    def test_basis_url_leitet_aufs_repo(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        for path in ("/", "/?token=x&user=y"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=5)
            self.assertEqual(cm.exception.code, 302)
            self.assertEqual(cm.exception.headers["Location"], "https://github.com/piLovr/Rapla-Splitter")

    def test_builder_seite_und_optionen(self):
        code, ctype, body = self.get("/builder")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("URL-Builder", body)
        self.assertEqual(self.get("/builder/")[0], 200)
        opts = json.loads(self.get("/builder/options.json")[2])
        self.assertEqual(opts["wahlfach_pool"], CFG["wahlfach_pool"])
        self.assertEqual(opts["optional"], CFG["optional"])
        self.assertEqual(list(opts["calendars"]), list(app.CALENDARS))

    def test_preview_json(self):
        code, ctype, body = self.get(f"/preview.json?{self.base}&wahl=Signale")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue({e["cal"] for e in data["events"]} <= set(app.CALENDARS))
        flat = {c: [e["title"] for e in data["events"] if e["cal"] == c] for c in app.CALENDARS}
        self.assertIn("Signale und Systeme", flat["vorlesungen"])
        self.assertIn("Linux Basics", flat["rest"])
        self.assertIn("Karfreitag", flat["rest"])
        self.assertTrue(any("Infoveranstaltung" in t for t in flat["veranstaltungen"]))
        self.assertGreater(flat["vorlesungen"].count("Signale und Systeme"), 3)  # Serie ist ausgeklappt
        e = next(e for e in data["events"] if e["cal"] == "vorlesungen")
        self.assertRegex(e["start"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")
        self.assertLess(e["start"], e["end"])
        self.assertEqual([n["keyword"] for n in data["nachklausuren"]], ["Prüfung - TINF25 - nur geladene Student*innen"])
        # das Stichwort aus der Vorschau funktioniert direkt als ?nach=
        kw = urllib.parse.quote(data["nachklausuren"][0]["keyword"])
        self.assertIn("nur geladene", self.get(f"/klausuren.ics?{self.base}&wahl=Signale&nach={kw}")[2])

    def test_preview_zeiten_sind_berliner_lokalzeit(self):
        data = json.loads(self.get(f"/preview.json?{self.base}")[2])
        stat = [e for e in data["events"] if e["title"] == "Statistik" and e["start"].startswith("2026-11-05")]
        self.assertEqual([(e["start"], e["end"]) for e in stat], [("2026-11-05T08:30", "2026-11-05T11:45")])

    def test_preview_bausteine_reichen_fuer_neuzuordnung_im_browser(self):
        """Der Builder ordnet beim Anhaken selbst neu zu (gleiche Regel wie hier); das muss assign() entsprechen."""
        raw = json.loads(self.get(f"/preview.json?{self.base}")[2])["events"]
        pool, keys = CFG["wahlfach_pool"], [n["keyword"] for n in json.loads(self.get(f"/preview.json?{self.base}")[2])["nachklausuren"]]

        def client(e, wahl, opts, nach):  # Spiegel von assignClient() in builder.html
            if e["kind"] == "rest":
                return "rest"
            if wahl and e["pool"] and not set(e["pool"]) & wahl:
                return "rest"
            if any(o not in opts for o in e["opts"]):
                return "rest"
            if e["kind"] == "nachklausur":
                return "klausuren" if "alle" in nach or set(e["nach"]) & nach else "rest"
            return e["kind"]

        combos = [(set(), set(), set()), ({pool[0]}, set(), set()), ({pool[4], pool[5]}, {"ccna"}, set(keys)),
                  ({pool[1]}, set(), {"alle"}), (set(pool), {"ccna"}, set())]
        for wahl, opts, nach in combos:
            q = self.base + (f"&wahl={urllib.parse.quote(','.join(sorted(wahl)))}" if wahl else "")
            q += "".join(f"&{o}=1" for o in opts) + (f"&nach={urllib.parse.quote(','.join(sorted(nach)))}" if nach else "")
            server = json.loads(self.get(f"/preview.json?{q}")[2])["events"]
            self.assertEqual([e["cal"] for e in server], [client(e, wahl, opts, nach) for e in raw], (wahl, opts, nach))

    def test_preview_fehler(self):
        self.assertEqual(self.get("/preview.json?file=25B6")[0], 400)
        self.assertEqual(self.get(f"/preview.json?{self.base}&wahl=Quanten")[0], 400)


class Cache(unittest.TestCase):
    """fetch() gegen einen lokalen Fake-Rapla: zaehlt Abrufe."""

    mode = "ok"

    @classmethod
    def setUpClass(cls):
        cls.hits = 0

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits += 1
                time.sleep(0.2)
                if cls.mode == "down":
                    self.send_response(500)
                    self.end_headers()
                    return
                data = TEXT.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cfg = dict(CFG, upstream=f"http://127.0.0.1:{cls.srv.server_address[1]}/ical", cache_seconds=3600, retry_seconds=900)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self._saved = (app.CFG, dict(app._cache), app.fetch)
        app.fetch = REAL_FETCH
        app.CFG = dict(self.cfg)
        app._cache.clear()
        type(self).hits, type(self).mode = 0, "ok"

    def tearDown(self):
        app.CFG, app.fetch = self._saved[0], self._saved[2]
        app._cache.clear()
        app._cache.update(self._saved[1])

    def test_zweiter_abruf_kommt_aus_dem_cache_auch_fuer_anderen_user(self):
        a = app.fetch("a", "25B6")
        b = app.fetch("b", "25B6")
        self.assertEqual(a, b)
        self.assertEqual(self.hits, 1)
        app.fetch("a", "25B1")  # anderer Kurs -> neuer Abruf
        self.assertEqual(self.hits, 2)

    def test_gleichzeitige_anfragen_teilen_sich_einen_abruf(self):
        threads = [threading.Thread(target=app.fetch, args=("a", "25B6")) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.hits, 1)

    def test_nach_ablauf_neu_holen_und_bei_ausfall_veraltete_kopie(self):
        app.fetch("a", "25B6")
        app._cache["25B6"] = (time.time() - 7200, app._cache["25B6"][1])  # abgelaufen
        type(self).mode = "down"
        self.assertEqual(app.fetch("a", "25B6"), TEXT)  # veraltete Kopie
        self.assertEqual(self.hits, 2)
        app.fetch("a", "25B6")  # Wartezeit (retry_seconds) laeuft: kein weiterer Abruf
        self.assertEqual(self.hits, 2)

    def test_ausfall_ohne_kopie_ist_fehler(self):
        type(self).mode = "down"
        with self.assertRaises(Exception):
            app.fetch("a", "25B6")


class Schutz(unittest.TestCase):
    """Rate-Limit, Sperre, Fehlerbehandlung. Nutzt eigene Limiter und Fake-Rapla."""

    @classmethod
    def setUpClass(cls):
        cls.upstream_hits = 0

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.upstream_hits += 1
                if "file=25B6" in self.path:
                    data, code = TEXT.encode(), 200
                else:
                    data, code = b"The calendar 'x' you tried to retrieve is not published or available.", 404
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.srv.shutdown()

    def setUp(self):
        self._saved = (app.CFG, app.LIMITER, dict(app._cache), dict(app._failed), app.fetch)
        app.fetch = REAL_FETCH
        app.CFG = dict(CFG, upstream=f"http://127.0.0.1:{self.fake.server_address[1]}/ical")
        app.CFG["rate_limit"] = dict(CFG["rate_limit"])
        app.LIMITER = app.Limiter()
        app._cache.clear()
        app._failed.clear()
        type(self).upstream_hits = 0

    def tearDown(self):
        app.CFG, app.LIMITER, app.fetch = self._saved[0], self._saved[1], self._saved[4]
        for d, saved in ((app._cache, self._saved[2]), (app._failed, self._saved[3])):
            d.clear()
            d.update(saved)

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def test_unbekannter_kurs_ist_404_mit_klartext_und_wird_nur_einmal_erfragt(self):
        for _ in range(3):
            code, body, _h = self.get("/preview.json?user=a&file=tinf25b6")
            self.assertEqual(code, 404)  # nicht 5xx: Cloudflare ersetzt sonst den Text durch eine HTML-Fehlerseite
            self.assertIn("not published", body)
        self.assertEqual(self.upstream_hits, 1)

    def test_upstream_ausfall_ist_502(self):
        app.CFG["upstream"] = "http://127.0.0.1:9/ical"
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6")[0], 502)

    def test_abruf_budget_fuehrt_zur_sperre(self):
        app.CFG["rate_limit"]["upstream_per_10min"] = 3
        codes = [self.get(f"/vorlesungen.ics?user=a&file=K{i}")[0] for i in range(3)]
        self.assertEqual(codes, [404, 404, 404])
        code, body, headers = self.get("/vorlesungen.ics?user=a&file=K99")
        self.assertEqual(code, 429)
        self.assertIn("Retry-After", headers)
        self.assertGreater(int(headers["Retry-After"]), 1000)
        # gesperrt: selbst gueltige Kurse kommen nicht durch, health und andere IPs schon
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6")[0], 429)
        self.assertEqual(self.get("/health")[0], 200)
        other = {"CF-Connecting-IP": "203.0.113.7"}
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6", other)[0], 200)

    def test_cache_treffer_verbrauchen_kein_budget(self):
        app.CFG["rate_limit"]["upstream_per_10min"] = 1
        for _ in range(5):
            self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6")[0], 200)

    def test_anfrage_limit_pro_minute(self):
        app.CFG["rate_limit"]["requests_per_minute"] = 3
        codes = [self.get("/builder/options.json")[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        self.assertEqual(self.get("/health")[0], 200)

    def test_limit_null_schaltet_ab(self):
        app.CFG["rate_limit"].update(requests_per_minute=0, upstream_per_10min=0)
        for i in range(5):
            self.assertEqual(self.get(f"/vorlesungen.ics?user=a&file=K{i}")[0], 404)

    def test_client_ip_header_wird_geprueft(self):
        app.CFG["rate_limit"]["requests_per_minute"] = 1
        self.assertEqual(self.get("/builder/options.json", {"CF-Connecting-IP": "198.51.100.1"})[0], 200)
        self.assertEqual(self.get("/builder/options.json", {"CF-Connecting-IP": "198.51.100.1"})[0], 429)
        self.assertEqual(self.get("/builder/options.json", {"CF-Connecting-IP": "198.51.100.2"})[0], 200)
        # Muell im Header faellt auf die Verbindungs-IP zurueck (keine Umgehung durch Fantasiewerte)
        self.assertEqual(self.get("/builder/options.json", {"CF-Connecting-IP": "nicht-eine-ip"})[0], 200)
        self.assertEqual(self.get("/builder/options.json", {"CF-Connecting-IP": "../../x"})[0], 429)

    def test_limiter_sperre_laeuft_ab(self):
        lim, cfg = app.Limiter(), {"requests_per_minute": 0, "upstream_per_10min": 1, "ban_seconds": 60}
        lim.upstream("1.2.3.4", cfg, now=1000)
        with self.assertRaises(app.RateLimited):
            lim.upstream("1.2.3.4", cfg, now=1001)
        with self.assertRaises(app.RateLimited):
            lim.request("1.2.3.4", cfg, now=1030)
        lim.request("1.2.3.4", cfg, now=1070)  # Sperre vorbei

    def test_handler_fehler_wird_500_statt_abgebrochener_verbindung(self):
        orig = app.build_preview
        app.build_preview = lambda *a, **k: 1 / 0
        try:
            code, body, _ = self.get("/preview.json?user=a&file=25B6")
        finally:
            app.build_preview = orig
        self.assertEqual(code, 500)

    def test_serienregel_mit_datum_als_until(self):
        from datetime import datetime
        start = datetime(2026, 10, 9, 8)
        for until in ("20261023", "20261023T060000Z", "20261023T080000"):
            got = app.icaltime.expansions(f"FREQ=WEEKLY;UNTIL={until}", set(), start)
            self.assertEqual(len(got), 3, until)
        self.assertEqual(app.icaltime.expansions("FREQ=WEEKLY;COUNT=abc", set(), start), [start])
        self.assertEqual(app.icaltime.expansions("FREQ=MONTHLY", set(), start), [start])


if __name__ == "__main__":
    unittest.main(verbosity=2)
