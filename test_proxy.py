"""Tests gegen einen echten Rapla-Export.

Aufruf:  RAPLA_ICS=/pfad/zur/25B6.ics python3 test_proxy.py
"""
import os
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app

ICS_PATH = os.environ.get("RAPLA_ICS", "25B6.ics")
CFG = app.load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
with open(ICS_PATH, encoding="utf-8") as f:
    TEXT = f.read()


def summaries(feed):
    return [app.parse_event(b)["summary"] for k, b in app.parse_items(feed) if k == "event"]


def feed(cal, wahl="", ccna=False):
    enabled = {"ccna"} if ccna else set()
    return app.build_feed(TEXT, cal, "25B6", wahl.split(",") if wahl else [], enabled, CFG)


class Filtering(unittest.TestCase):
    def test_vier_kalender_trennen_ohne_ueberschneidung(self):
        total = sum(1 for k, _ in app.parse_items(TEXT) if k == "event")
        parts = {c: summaries(feed(c, ccna=True)) for c in app.CALENDARS}
        kept = sum(len(v) for v in parts.values())
        dropped = [
            app.parse_event(b)["summary"]
            for k, b in app.parse_items(TEXT)
            if k == "event" and app.classify(app.parse_event(b), CFG) is None
        ]
        self.assertEqual(kept + len(dropped), total)
        self.assertTrue(all(any(x in d for x in ("Karfreitag", "Ostermontag", "Fschingsdienstag", "Beginn Theorie", "Klausurwoche")) for d in dropped), dropped)

    def test_semester_nur_blau_mit_ort(self):
        s = summaries(feed("semester"))
        self.assertEqual(sorted(x.split("   ")[0] for x in s), ["Programmieren - Übungstermin", "Semesterstart-Besprechung"])

    def test_feiertage_nirgends(self):
        for cal in app.CALENDARS:
            blob = " ".join(summaries(feed(cal, ccna=True)))
            for bad in ("Karfreitag", "Ostermontag", "Fschingsdienstag", "Klausurwoche", "Beginn Theorie"):
                self.assertNotIn(bad, blob, cal)

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

    def test_nachklausur(self):
        s = summaries(feed("nachklausuren", wahl="Linux"))
        self.assertEqual(len(s), 1)
        self.assertIn("nur geladene", s[0])

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
        app.TOKEN = "t" * 24
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

    base = "user=li%40dhbw-karlsruhe.aa&file=25B6&token=" + "t" * 24

    def test_ok(self):
        code, ctype, body = self.get(f"/vorlesungen.ics?{self.base}&wahl=Signale")
        self.assertEqual(code, 200)
        self.assertIn("text/calendar", ctype)
        self.assertIn("Signale und Systeme", body)
        self.assertNotIn("Linux Basics", body)

    def test_ccna_flag(self):
        self.assertNotIn("CCNA", self.get(f"/vorlesungen.ics?{self.base}")[2])
        self.assertIn("CCNA", self.get(f"/vorlesungen.ics?{self.base}&ccna=1")[2])

    def test_token(self):
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6")[0], 403)
        self.assertEqual(self.get("/vorlesungen.ics?user=a&file=25B6&token=falsch")[0], 403)

    def test_ungueltige_parameter(self):
        t = "t" * 24
        self.assertEqual(self.get(f"/vorlesungen.ics?file=25B6&token={t}")[0], 400)
        self.assertEqual(self.get(f"/vorlesungen.ics?user=a&file=../../etc&token={t}")[0], 400)
        code, _, body = self.get(f"/vorlesungen.ics?{self.base}&wahl=Quanten")
        self.assertEqual(code, 400)
        self.assertIn("Signale und Systeme", body)

    def test_routen(self):
        self.assertEqual(self.get("/health")[0], 200)
        self.assertEqual(self.get("/")[0], 404)
        self.assertEqual(self.get("/foo.ics")[0], 404)
        for cal in app.CALENDARS:
            self.assertEqual(self.get(f"/{cal}.ics?{self.base}")[0], 200, cal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
