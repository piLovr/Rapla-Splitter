#!/usr/bin/env python3
"""Wahlfaecher aller Studiengaenge finden.

Holt (wie der Roomfinder) die Kursliste und jeden Stundenplan, vergleicht darin die Vorlesungen und
erkennt Wahlfaecher daran, dass mehrere verschiedene Veranstaltungen im selben Kurs zur selben Zeit
stattfinden (man besucht nur eine davon). Ergebnis: pro Kurs Wahlgruppen, pro Studiengang die
Wahlfaecher, dazu ein fertiger `wahlfach_pool` fuer config.yaml.

  python3 wahlfaecher.py                         # alles scrapen, Bericht ausgeben
  python3 wahlfaecher.py --json out.json         # zusaetzlich als JSON speichern
  python3 wahlfaecher.py --cache ics-cache       # heruntergeladene Plaene wiederverwenden
  python3 wahlfaecher.py --ics 25B6.ics ...      # nur lokale .ics-Dateien auswerten (kein Netz)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import app
import icaltime
from icaltime import berlin_to_utc, expansions, utc_to_berlin  # noqa: F401 (von Tests genutzt)

COURSES_URL = "https://api.dhbw.app/courses/{site}/"
ICAL_URL = "https://dhbw.app/ical/{course}"
USER_AGENT = "rapla-splitter-wahlfaecher/1.0"  # dhbw.app weist Pythons Standard-User-Agent mit 403 ab

# Kein Wahlfach-Kandidat: Pruefungen, Besprechungen, Feiertage, Ersti-Termine, Raumblocker, parallele Gruppen ...
NOT_A_LECTURE = re.compile(
    r"^\W*(klausur|wkl\b|nkl\b|wdh|wiederholung|nach-? ?bzw|nachklausur|portfolio|"
    r"beginn\b|ersti|evaluation|semester\W*(start|end)|\d+\. ?semester|raum\b|vorbereitung|vorstellung)|"
    r"pr[uü]fung|kolloquium|klausurwoche|besprechung|gespr[aä]ch|feiertag|exkursion|tutorium|aufenthaltsraum|"
    r"\bgrupp(e|en)\b|\bgroup\b|supervision",
    re.I,
)
# Klammern/Zusaetze und fuehrende Satzzeichen entfernen: 'Bildverarbeitung (KI & BV)' -> 'Bildverarbeitung'
VARIANT = re.compile(r"\s*[(<\[][^)>\]]*[)>\]]|^\W+")
# 'Gruppe A' / 'Group 2' / 'Gr. B' am Ende: parallele Gruppen desselben Fachs, kein Wahlfach
GROUP_SUFFIX = re.compile(r"[\s\-_(]*\b(gruppe|group|gr\.?|kurs|teil|part)?\s*([a-d]|\d{1,2}|i{1,3})\)?\s*$", re.I)

MIN_OVERLAPS = 2  # so oft muessen zwei Faecher zusammenfallen, bevor sie als Alternativen gelten
MIN_RATIO = 0.5  # ... und mindestens so viel Anteil des haeufigeren der beiden Faecher


# --------------------------------------------------------------------------
# Stundenplan -> Vorlesungen
# --------------------------------------------------------------------------
def parse_time(group):
    """-> lokale (Berliner) Zeit oder None fuer ganztaegige Termine (VALUE=DATE)."""
    return icaltime.parse_value(app.prop_value(group))


def exdate_times(block):
    return icaltime.exdate_times([app.prop_value(g) for g in app.logical_lines(block) if app.prop_name(g) == "EXDATE"])


def clean_title(s):
    return re.sub(r"\s+", " ", s).strip()


def lectures(text, ignore=()):
    """-> [(start, end, titel)] aller Termine, die wie Vorlesungen aussehen (Zeiten in UTC)."""
    ignore = [app.norm(i) for i in ignore]
    out = []
    for kind, block in app.parse_items(text):
        if kind != "event":
            continue
        times, rrule, ev = {}, "", app.parse_event(block)
        for g in app.logical_lines(block):
            name = app.prop_name(g)
            if name in ("DTSTART", "DTEND") and name not in times:
                times[name] = parse_time(g)
            elif name == "RRULE" and not rrule:
                rrule = app.prop_value(g)
        title = clean_title(ev["summary"])
        if not title or "feiertage" in ev["categories"] or NOT_A_LECTURE.search(title):
            continue
        if any(i in app.norm(title) for i in ignore):
            continue
        start, end = times.get("DTSTART"), times.get("DTEND")
        if not (start and end and end > start):
            continue
        for t in expansions(rrule, exdate_times(block), start) if rrule else [start]:
            out.append((berlin_to_utc(t), berlin_to_utc(t + (end - start)), title))
    return out


# --------------------------------------------------------------------------
# Wahlgruppen erkennen
# --------------------------------------------------------------------------
def group_base(title):
    return app.norm(GROUP_SUFFIX.sub("", title))


def overlaps(events):
    """Zaehlt je Titelpaar, wie oft sich Termine zeitlich ueberschneiden."""
    pairs = Counter()
    events = sorted(events)
    for i, (s1, e1, t1) in enumerate(events):
        for s2, e2, t2 in events[i + 1:]:
            if s2 >= e1:
                break
            if t1 != t2:
                pairs[tuple(sorted((t1, t2)))] += 1
    return pairs


def elective_groups(events):
    """-> Liste von Wahlgruppen, jede = sortierte Liste (titel, anzahl_termine)."""
    counts = Counter(t for _, _, t in events)
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked = set()
    for (a, b), n in overlaps(events).items():
        if n < MIN_OVERLAPS or n / max(counts[a], counts[b]) < MIN_RATIO:
            continue
        if group_base(a) == group_base(b):
            continue  # nur parallele Uebungsgruppen
        parent[find(a)] = find(b)
        linked.update((a, b))
    comps = defaultdict(list)
    for t in linked:
        comps[find(t)].append(t)
    return sorted(
        (sorted((t, counts[t]) for t in c) for c in comps.values() if len(c) > 1),
        key=lambda g: g[0][0],
    )


# --------------------------------------------------------------------------
# Studiengaenge
# --------------------------------------------------------------------------
PROGRAM_RE = re.compile(r"^[A-Z]{2}-([A-ZÄÖÜ]{2,}?)(\d{2})(?!\d)(.*)$")


def program_of(course):
    """'KA-TINF25B6' -> ('TINF', '25'); alles ohne Jahrgang (Raeume, Sonderkalender) -> (kurs, '')."""
    m = PROGRAM_RE.match(course)
    return (m.group(1), m.group(2)) if m else (course, "")


def canonical(title):
    return clean_title(re.sub(r"\s+:", ":", VARIANT.sub("", title))) or title


def merge_variants(titles):
    """Schreibvarianten desselben Fachs zusammenfuehren ('Bildverarbeitung'/'Bildverarbetiung'); der haeufigste Name gewinnt.
    titles: {name: set(kurse)}"""
    merged = {}
    for name in sorted(titles, key=lambda t: (-len(titles[t]), t)):
        for keep in merged:
            if app.norm(name) == app.norm(keep) or SequenceMatcher(None, app.norm(name), app.norm(keep)).ratio() >= 0.93:
                merged[keep] |= titles[name]
                break
        else:
            merged[name] = set(titles[name])
    return merged


def analyze(schedules, ignore=(), since=None):
    """schedules: {kurs: ics-text} -> Bericht (dict). `since` (UTC): Kurse ohne Vorlesung ab dann gelten als abgeschlossen."""
    per_course = {}
    for course, text in schedules.items():
        events = lectures(text, ignore)
        if since and not any(end >= since for _, end, _ in events):
            continue
        groups = elective_groups(events)
        if groups:
            per_course[course] = groups
    programs = defaultdict(lambda: defaultdict(set))  # studiengang -> fach -> kurse
    for course, groups in per_course.items():
        prog, _ = program_of(course)
        for g in groups:
            for title, _n in g:
                programs[prog][canonical(title)].add(course)
    return {
        "courses_scanned": len(schedules),
        "courses": {c: [[{"title": t, "events": n} for t, n in g] for g in gs] for c, gs in sorted(per_course.items())},
        "programs": {
            p: {t: sorted(cs) for t, cs in sorted(merge_variants(titles).items())}
            for p, titles in sorted(programs.items())
        },
    }


def pool_yaml(report):
    """Wahlfaecher je Studiengang als YAML. (Nicht in einen gemeinsamen Pool werfen: 'Informatik' wuerde
    sonst auch 'Technische Informatik' erwischen.)"""
    out = "wahlfaecher:\n"
    for prog, titles in report["programs"].items():
        out += f"  {prog}:\n" + "".join(f"    - {t}\n" for t in titles)
    return out


# --------------------------------------------------------------------------
# Scraping (hoeflich: ein Abruf nach dem anderen, mit Pause)
# --------------------------------------------------------------------------
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_courses(site="KA", courses_url=COURSES_URL):
    return json.loads(http_get(courses_url.format(site=site)))


def fetch_all(courses, ical_url=ICAL_URL, delay=0.15, cache=None, log=print):
    """-> ({kurs: ics}, [fehlgeschlagene kurse])"""
    out, failed = {}, []
    for course in courses:
        path = os.path.join(cache, course + ".ics") if cache else None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                out[course] = f.read()
            continue
        try:
            text = http_get(ical_url.format(course=urllib.parse.quote(course)))
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("kein iCal")
        except Exception as e:  # noqa: BLE001
            failed.append(course)
            log(f"  {course}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        out[course] = text
        if path:
            os.makedirs(cache, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        time.sleep(delay)
    return out, failed


def print_report(report, file=sys.stdout):
    print(f"{report['courses_scanned']} Kurse ausgewertet, {len(report['courses'])} mit Wahlfaechern\n", file=file)
    for prog, titles in report["programs"].items():
        print(f"{prog}", file=file)
        for t, cs in titles.items():
            print(f"  {t}  ({', '.join(c.split('-', 1)[-1] for c in cs)})", file=file)
    print("\n" + pool_yaml(report), file=file)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default="KA", help="Standort-Praefix der Kursliste (Standard: KA)")
    ap.add_argument("--delay", type=float, default=0.15, help="Pause zwischen Abrufen in Sekunden")
    ap.add_argument("--since", help="nur Kurse mit Vorlesungen ab diesem Datum (YYYY-MM-DD), blendet abgeschlossene Jahrgaenge aus")
    ap.add_argument("--cache", help="Ordner, in dem Stundenplaene zwischengespeichert werden")
    ap.add_argument("--json", help="Bericht zusaetzlich als JSON speichern")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    ap.add_argument("--ics", nargs="+", help="Lokale .ics-Dateien statt Scraping")
    args = ap.parse_args()
    cfg = app.load_config(args.config)

    if args.ics:
        schedules = {}
        for p in args.ics:
            with open(p, encoding="utf-8") as f:
                schedules[os.path.splitext(os.path.basename(p))[0]] = f.read()
    else:
        courses = fetch_courses(args.site)
        print(f"{len(courses)} Kurse gefunden, lade Stundenplaene ...", file=sys.stderr)
        schedules, failed = fetch_all(courses, delay=args.delay, cache=args.cache, log=print)
        if failed:
            print(f"{len(failed)} Kurse nicht abrufbar: {', '.join(failed)}", file=sys.stderr)

    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    report = analyze(schedules, cfg["ignore"], since)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    print_report(report)


if __name__ == "__main__":
    main()
