"""Tests fuer wahlfaecher.py. Laufen offline; der Scraper wird gegen einen lokalen Mini-Server geprueft.

Aufruf:  python3 test_wahlfaecher.py
Optional gegen echten Rapla-Export:  RAPLA_ICS=/pfad/zur/25B6.ics python3 test_wahlfaecher.py
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app
import wahlfaecher as w

CFG = app.load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))


def ics(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def ev(title, start, hours=3, loc="A266", cat=None, extra=""):
    """Termin in Berliner Lokalzeit (start = datetime)."""
    end = start + timedelta(hours=hours)
    return (
        "BEGIN:VEVENT\r\nUID:1\r\n"
        f"DTSTART;TZID=Europe/Berlin:{start:%Y%m%dT%H%M%S}\r\nDTEND;TZID=Europe/Berlin:{end:%Y%m%dT%H%M%S}\r\n"
        f"SUMMARY: {title}  \r\nLOCATION:{loc}\r\nCATEGORIES:{cat or title}\r\n{extra}END:VEVENT\r\n"
    )


def weekly(title, first, n, **kw):
    return [ev(title, first + timedelta(weeks=i), **kw) for i in range(n)]


FIRST = datetime(2026, 10, 9, 8, 0)  # Freitag


def semester(electives=("Signale und Systeme", "Linux Basics", "Geschäftsprozesse")):
    """Pflichtfach jede Woche + Wahlfaecher parallel zueinander, jede Woche eins davon (mit je 6 Terminen)."""
    events = weekly("Netztechnik", FIRST + timedelta(days=1, hours=5), 6)
    for i, name in enumerate(electives):
        events += weekly(name, FIRST + timedelta(hours=i * 0.5), 6)
    return events


def groups(text, **kw):
    return [[t for t, _ in g] for g in w.elective_groups(w.lectures(text, **kw))]


class Zeiten(unittest.TestCase):
    def test_sommer_und_winterzeit(self):
        self.assertEqual(w.berlin_to_utc(datetime(2026, 7, 1, 10)), datetime(2026, 7, 1, 8))
        self.assertEqual(w.berlin_to_utc(datetime(2026, 12, 1, 10)), datetime(2026, 12, 1, 9))
        self.assertEqual(w.utc_to_berlin(datetime(2026, 7, 1, 8)), datetime(2026, 7, 1, 10))
        # Umstellung 2026: 29.3. und 25.10.
        self.assertEqual(w.berlin_to_utc(datetime(2026, 3, 29, 12)), datetime(2026, 3, 29, 10))
        self.assertEqual(w.berlin_to_utc(datetime(2026, 3, 28, 12)), datetime(2026, 3, 28, 11))
        self.assertEqual(w.berlin_to_utc(datetime(2026, 10, 25, 12)), datetime(2026, 10, 25, 11))
        self.assertEqual(w.berlin_to_utc(datetime(2026, 10, 24, 12)), datetime(2026, 10, 24, 10))

    def test_roundtrip(self):
        for t in (datetime(2026, 1, 5, 8), datetime(2026, 6, 5, 8), datetime(2026, 10, 30, 23)):
            self.assertEqual(w.utc_to_berlin(w.berlin_to_utc(t)), t)

    def test_serientermin_mit_ausnahme(self):
        start = datetime(2026, 10, 9, 8)
        block = f"EXDATE;TZID=Europe/Berlin:{(start + timedelta(weeks=1)):%Y%m%dT%H%M%S}\r\n"
        got = w.expansions("FREQ=WEEKLY;COUNT=4;INTERVAL=1;BYDAY=FR", w.exdate_times([block]), start)
        self.assertEqual(got, [start, start + timedelta(weeks=2), start + timedelta(weeks=3)])

    def test_serientermin_ueber_zeitumstellung_bleibt_um_8(self):
        text = ics(ev("X", datetime(2026, 10, 9, 8), extra="RRULE:FREQ=WEEKLY;COUNT=3;INTERVAL=1;BYDAY=FR\r\n"))
        starts = sorted(s for s, _, _ in w.lectures(text))
        self.assertEqual([w.utc_to_berlin(s).hour for s in starts], [8, 8, 8])
        self.assertEqual(len(starts), 3)

    def test_ganztaegig_wird_uebersprungen(self):
        e = "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261003\r\nDTEND;VALUE=DATE:20261004\r\nSUMMARY:Foo\r\nEND:VEVENT\r\n"
        self.assertEqual(w.lectures(ics(e)), [])


class Erkennung(unittest.TestCase):
    def test_wahlgruppe_gefunden_pflichtfach_nicht(self):
        g = groups(ics(*semester()))
        self.assertEqual(g, [["Geschäftsprozesse", "Linux Basics", "Signale und Systeme"]])

    def test_pflichtfaecher_ohne_ueberschneidung_sind_keine_wahlfaecher(self):
        events = weekly("A", FIRST, 6) + weekly("B", FIRST + timedelta(hours=4), 6)
        self.assertEqual(groups(ics(*events)), [])

    def test_keine_wahlfaecher_bei_einzelner_ueberschneidung(self):
        events = [ev("A", FIRST), ev("B", FIRST + timedelta(hours=1))]
        self.assertEqual(groups(ics(*events)), [])

    def test_parallele_gruppen_sind_kein_wahlfach(self):
        events = weekly("Intercultural Communication Group A", FIRST, 6) + weekly("Intercultural Communication Group B", FIRST, 6)
        events += weekly("Labor Gruppe 1", FIRST, 6) + weekly("Labor Gruppe 2", FIRST, 6)
        events += weekly("Englisch 1", FIRST, 6) + weekly("Englisch 2", FIRST, 6)
        self.assertEqual(groups(ics(*events)), [])

    def test_pruefungen_feiertage_besprechungen_zaehlen_nicht(self):
        events = []
        for name in ("Klausur Statistik", "Prüfung Programmieren", "Semesterstart-Besprechung", "Karfreitag", "Klausurwoche 3. Semester"):
            events += weekly(name, FIRST, 6)
        events += weekly("Statistik", FIRST, 6)
        self.assertEqual(groups(ics(*events), ignore=CFG["ignore"]), [])

    def test_ccna_nur_teilweise_parallel_ist_kein_wahlfach(self):
        # CCNA laeuft neben Netztechnik, das aber 18x stattfindet -> Anteil < 50 %
        events = weekly("Netztechnik", FIRST, 18) + weekly("CCNA-2", FIRST, 6)
        self.assertEqual(groups(ics(*events)), [])

    def test_zwei_getrennte_wahlbloecke(self):
        later = FIRST + timedelta(days=-4, hours=6)
        events = semester() + weekly("Robotik", later, 6) + weekly("Workflow", later, 6) + weekly("OO Best Practice", later, 6)
        self.assertEqual(len(groups(ics(*events))), 2)

    def test_ignore_liste_aus_config(self):
        events = semester(("Signale und Systeme", "Linux Basics", "Feiertag Spezial"))
        self.assertEqual(groups(ics(*events), ignore=CFG["ignore"]), [["Linux Basics", "Signale und Systeme"]])

    def test_zeiten_in_z_und_lokal_werden_verglichen(self):
        # 08:00 lokal (Sommer) == 06:00Z
        loc = ev("A", datetime(2026, 7, 3, 8))
        utc = "BEGIN:VEVENT\r\nDTSTART:20260703T060000Z\r\nDTEND:20260703T090000Z\r\nSUMMARY:B\r\nEND:VEVENT\r\n"
        self.assertEqual(w.overlaps(w.lectures(ics(loc, utc))), {("A", "B"): 1})


class Studiengaenge(unittest.TestCase):
    def test_program_of(self):
        self.assertEqual(w.program_of("KA-TINF25B6"), ("TINF", "25"))
        self.assertEqual(w.program_of("KA-WWI23INT"), ("WWI", "23"))
        self.assertEqual(w.program_of("KA-INTERNATIONAL-STUDENTS"), ("KA-INTERNATIONAL-STUDENTS", ""))
        self.assertEqual(w.program_of("KA-C239-HÖRSAAL"), ("KA-C239-HÖRSAAL", ""))

    def test_canonical_und_varianten(self):
        self.assertEqual(w.canonical("Bildverarbeitung (KI & BV)"), "Bildverarbeitung")
        self.assertEqual(w.canonical(".Grundlagen der KI (KI & BV)"), "Grundlagen der KI")
        self.assertEqual(w.canonical("Wahlmodul (SC/SE): Mobile Applikationen I"), "Wahlmodul: Mobile Applikationen I")
        merged = w.merge_variants({"Bildverarbeitung": {"a", "b"}, "Bildverarbetiung": {"c"}, "Robotik": {"a"}})
        self.assertEqual(sorted(merged), ["Bildverarbeitung", "Robotik"])
        self.assertEqual(merged["Bildverarbeitung"], {"a", "b", "c"})

    def test_analyze_pro_studiengang(self):
        sched = {
            "KA-TINF25B1": ics(*semester()),
            "KA-TINF25B2": ics(*semester()),
            "KA-WWI25B1": ics(*semester(("Wahlmodul: Mobile", "Wahlmodul: Daten", "Wahlmodul: KI"))),
            "KA-TMB25B1": ics(*weekly("Mathe", FIRST, 6)),
        }
        r = w.analyze(sched)
        self.assertEqual(r["courses_scanned"], 4)
        self.assertEqual(sorted(r["courses"]), ["KA-TINF25B1", "KA-TINF25B2", "KA-WWI25B1"])
        self.assertEqual(sorted(r["programs"]), ["TINF", "WWI"])
        self.assertEqual(r["programs"]["TINF"]["Linux Basics"], ["KA-TINF25B1", "KA-TINF25B2"])
        self.assertIn("Wahlmodul: KI", r["programs"]["WWI"])
        self.assertIn("  TINF:\n    - Geschäftsprozesse\n", w.pool_yaml(r))

    def test_since_blendet_abgeschlossene_kurse_aus(self):
        old = [ev(n, datetime(2024, 10, 11, 8) + timedelta(weeks=i, hours=h)) for n, h in (("Ethik", 0), ("Forensik", 0.5)) for i in range(6)]
        sched = {"KA-TINF22B1": ics(*old), "KA-TINF25B1": ics(*semester())}
        self.assertEqual(sorted(w.analyze(sched)["courses"]), ["KA-TINF22B1", "KA-TINF25B1"])
        self.assertEqual(sorted(w.analyze(sched, since=datetime(2026, 1, 1))["courses"]), ["KA-TINF25B1"])


class Scraper(unittest.TestCase):
    """Mini-DHBW-Server: Kursliste + Stundenplaene, ein Kurs liefert 403, einer Muell."""

    hits = []

    @classmethod
    def setUpClass(cls):
        plans = {"KA-TINF25B1": ics(*semester()), "KA-TINF25B2": ics(*semester()), "KA-GEHT-NICHT": "", "KA-MUELL": "<html>"}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits.append((self.path, self.headers.get("User-Agent")))
                if self.path == "/courses/KA/":
                    return self._send(200, json.dumps(list(plans)))
                name = self.path.rsplit("/", 1)[-1]
                if name == "KA-GEHT-NICHT" or "Python" in (self.headers.get("User-Agent") or ""):
                    return self._send(403, "forbidden")
                self._send(200, plans.get(name, ""))

            def _send(self, code, body):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        type(self).hits = []

    def scrape(self, **kw):
        courses = w.fetch_courses("KA", self.base + "/courses/{site}/")
        return w.fetch_all(courses, ical_url=self.base + "/ical/{course}", delay=0, log=lambda *a, **k: None, **kw)

    def test_kursliste_und_plaene(self):
        plans, failed = self.scrape()
        self.assertEqual(sorted(plans), ["KA-TINF25B1", "KA-TINF25B2"])
        self.assertEqual(sorted(failed), ["KA-GEHT-NICHT", "KA-MUELL"])
        self.assertEqual(w.analyze(plans)["programs"]["TINF"]["Linux Basics"], ["KA-TINF25B1", "KA-TINF25B2"])

    def test_eigener_user_agent(self):
        self.scrape()
        self.assertTrue(all(ua == w.USER_AGENT for _, ua in self.hits), self.hits)

    def test_cache_vermeidet_zweiten_abruf(self):
        with tempfile.TemporaryDirectory() as d:
            self.scrape(cache=d)
            first = len(self.hits)
            plans, _ = self.scrape(cache=d)
            self.assertEqual(sorted(plans), ["KA-TINF25B1", "KA-TINF25B2"])
            # Kursliste + nur die zwei zuvor fehlgeschlagenen Kurse werden erneut angefragt
            self.assertEqual(len(self.hits) - first, 1 + 2)


@unittest.skipUnless(os.environ.get("RAPLA_ICS"), "RAPLA_ICS nicht gesetzt")
class EchterExport(unittest.TestCase):
    def test_tinf25_wahlpool_entspricht_config(self):
        with open(os.environ["RAPLA_ICS"], encoding="utf-8") as f:
            found = groups(f.read(), ignore=CFG["ignore"])
        self.assertEqual(len(found), 1, found)
        # config.yaml: Signale, Digitale Waehrungssysteme, Geschaeftsprozesse, Webengineering 3, Linux Basics
        expected = {app.norm(e) for e in CFG["wahlfach_pool"][:5]}
        self.assertEqual({app.norm(t) for t in found[0]}, expected)


if __name__ == "__main__":
    unittest.main()
