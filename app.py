#!/usr/bin/env python3
"""Rapla iCal filter proxy.

Holt den iCal-Feed von Rapla (DHBW) und teilt ihn in vier Kalender auf, die sich nicht ueberschneiden und
zusammen alles aus Rapla enthalten:
  /vorlesungen.ics      Lehrveranstaltungen
  /veranstaltungen.ics  alles Uebrige (Semesterstart, Infoveranstaltungen ...); alter Pfad /semester.ics geht weiter
  /klausuren.ics        Pruefungen (dazu die gewaehlten Nachklausuren)
  /rest.ics             Feiertage, nicht belegte Wahlfaecher/Optionals samt Klausuren, nicht gewaehlte Nachklausuren
Weitere Seiten:
  /                     leitet auf das GitHub-Repo weiter
  /builder              grafischer URL-Builder mit Vorschau

Query-Parameter (fuer alle vier Kalender gleich):
  user    Pflicht, Rapla-User (wird 1:1 an Rapla durchgereicht)
  file    Pflicht, Rapla-Kurs (z. B. 25B6)
  wahl    Stichwort des gewaehlten Wahlfachs, z. B. wahl=Signale (mehrere mit Komma)
  ccna    ccna=1 nimmt optionale Faecher mit auf (Schluessel stehen in config.yaml)
  nach    Stichworte der Nachklausuren, die man schreiben muss (mehrere mit Komma, nach=alle nimmt alle);
          sie landen in klausuren.ics statt in rest.ics
  token   wird ignoriert (alte URLs funktionieren weiter)
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.request
from datetime import timedelta
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import yaml

import icaltime

MAX_UPSTREAM_BYTES = 20 * 1024 * 1024
USER_RE = re.compile(r"[A-Za-z0-9._@+\-]{1,80}")
FILE_RE = re.compile(r"[A-Za-z0-9_\-]{1,40}")
CALENDARS = {
    "vorlesungen": "Vorlesungen",
    "veranstaltungen": "Veranstaltungen",
    "klausuren": "Klausuren",
    "rest": "Rest",
}
ALIASES = {"semester": "veranstaltungen"}  # alter Pfad
REPO_URL = "https://github.com/piLovr/Rapla-Splitter"
HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("cache_seconds", 43200)
    cfg.setdefault("retry_seconds", 900)
    cfg.setdefault("wahlfach_pool", [])
    cfg.setdefault("optional", {})
    cfg.setdefault("ignore", [])
    cfg["categories"] = {
        "lecture": "Lehrveranstaltung",
        "exam": "Prüfung",
        "other": "Sonstiger Termin",
        **(cfg.get("categories") or {}),
    }
    match = cfg.get("nachklausur_match", "geladene")
    cfg["nachklausur_match"] = [match] if isinstance(match, str) else list(match)
    return cfg


ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
ROMAN_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(sorted(ROMAN, key=len, reverse=True)) + r")(?![a-z0-9])")


def norm(s):
    """Klein, ohne Umlaute/Sonderzeichen/Leerzeichen; roemische Zahlen werden arabisch:
    'Web Engineering III' == 'webengineering3'."""
    s = ROMAN_RE.sub(lambda m: ROMAN[m.group(1)], s.casefold())
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def split_lines(text):
    """Zeilen MIT Zeilenende, damit CRLF/LF unveraendert bleibt."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def parse_items(text):
    """-> Liste aus ('raw', zeile) und ('event', [zeilen])."""
    items, cur = [], None
    for ln in split_lines(text):
        if cur is None:
            if ln.startswith("BEGIN:VEVENT"):
                cur = [ln]
            else:
                items.append(("raw", ln))
        else:
            cur.append(ln)
            if ln.startswith("END:VEVENT"):
                items.append(("event", cur))
                cur = None
    return items  # ein nicht abgeschlossenes VEVENT wird verworfen


def logical_lines(block):
    """Fasst gefaltete Zeilen (beginnen mit Leerzeichen/Tab) zusammen."""
    out = []
    for ln in block:
        if ln[:1] in (" ", "\t") and out:
            out[-1].append(ln)
        else:
            out.append([ln])
    return out


def prop_name(group):
    m = re.match(r"[A-Za-z0-9\-]+", group[0])
    return m.group(0).upper() if m else ""


def prop_value(group):
    first = group[0].rstrip("\r\n")
    parts = [first] + [g.rstrip("\r\n")[1:] for g in group[1:]]
    joined = "".join(parts)
    _, _, value = joined.partition(":")
    value = re.sub(r"\\([,;nN\\])", lambda m: " " if m.group(1) in "nN" else m.group(1), value)
    return value


def parse_event(block):
    props = {}
    for g in logical_lines(block):
        name = prop_name(g)
        if name in ("SUMMARY", "LOCATION", "CATEGORIES") and name not in props:
            props[name] = prop_value(g)
    return {
        "summary": props.get("SUMMARY", ""),
        "location": props.get("LOCATION", ""),
        "categories": {c.strip().casefold() for c in props.get("CATEGORIES", "").split(",") if c.strip()},
    }


def dedupe_dtstamp(block):
    """Rapla schreibt DTSTAMP doppelt (laut RFC 5545 ungueltig) -> nur das erste behalten."""
    seen, out = False, []
    for g in logical_lines(block):
        if prop_name(g) == "DTSTAMP":
            if seen:
                continue
            seen = True
        out.extend(g)
    return out


def classify(ev, cfg):
    """Grundzuordnung ohne Wahlfach-/Optional-Auswahl: vorlesungen | klausuren | nachklausur | veranstaltungen | rest."""
    cats = cfg["categories"]
    if cats["lecture"].casefold() in ev["categories"]:
        return "vorlesungen"
    title = norm(ev["summary"])
    if cats["exam"].casefold() in ev["categories"]:
        if any(norm(p) in title for p in cfg["nachklausur_match"]):
            return "nachklausur"
        return "klausuren"
    if any(norm(p) in title for p in cfg["ignore"]):
        return "rest"  # Feiertage, "Beginn Theorie", "Klausurwoche"
    return "veranstaltungen"  # alles Uebrige, damit kein Termin durchrutscht


def wants_nach(ev, nach_keywords):
    title = norm(ev["summary"])
    return any(norm(k) == "alle" or (norm(k) and norm(k) in title) for k in nach_keywords)


def assign(ev, chosen, enabled_opts, nach_keywords, cfg):
    """Genau ein Kalender pro Termin: nichts geht verloren, nichts doppelt."""
    kind = classify(ev, cfg)
    if kind == "rest" or not selected(ev, chosen, enabled_opts, cfg):
        return "rest"
    if kind == "nachklausur":
        return "klausuren" if wants_nach(ev, nach_keywords) else "rest"
    return kind


def resolve_wahl(keywords, pool):
    chosen = set()
    for kw in keywords:
        k = norm(kw)
        if not k:
            continue
        hits = [e for e in pool if k in norm(e)]
        if len(hits) != 1:
            opts = ", ".join(pool)
            why = "passt auf kein Wahlfach" if not hits else "passt auf mehrere Wahlfaecher"
            raise ValueError(f"wahl='{kw}' {why}. Moegliche Fachnamen (config.yaml): {opts}")
        chosen.add(hits[0])
    return chosen


def title_has(name, title):
    """norm(name) steckt im Titel, auch mit einem Tippfehler (Rapla: 'Webengineeering III')."""
    n = norm(name)
    if n in title:
        return True
    if len(n) < 8:
        return False
    for size in (len(n) - 1, len(n), len(n) + 1):
        for i in range(len(title) - size + 1):
            if SequenceMatcher(None, n, title[i:i + size]).ratio() >= 0.9:
                return True
    return False


def selected(ev, chosen, enabled_opts, cfg):
    title = norm(ev["summary"])
    if chosen:
        matched = {e for e in cfg["wahlfach_pool"] if title_has(e, title)}
        if matched and not (matched & chosen):
            return False
    for key, pattern in cfg["optional"].items():
        if norm(pattern) in title and key not in enabled_opts:
            return False
    return True


def build_feed(text, cal, file_label, wahl_keywords, enabled_opts, cfg, nach_keywords=()):
    """Gibt den gefilterten iCal-Text fuer einen der vier Kalender zurueck."""
    chosen = resolve_wahl(wahl_keywords, cfg["wahlfach_pool"])
    out, header_done = [], False
    for kind, data in parse_items(text):
        if kind == "raw":
            out.append(data)
            if not header_done and data.startswith("VERSION:"):
                eol = "\r\n" if data.endswith("\r\n") else "\n"
                out.append(f"X-WR-CALNAME:{file_label} {CALENDARS[cal]}{eol}")
                out.append(f"X-PUBLISHED-TTL:PT1H{eol}")
                header_done = True
            continue
        ev = parse_event(data)
        if assign(ev, chosen, enabled_opts, nach_keywords, cfg) != cal:
            continue
        out.extend(dedupe_dtstamp(data))
    return "".join(out)


def nach_keyword(title):
    """Kurzes Stichwort fuer eine Nachklausur ('Wdh-Klausur Statistik (90 min)|..' -> 'Wdh-Klausur Statistik')."""
    return re.split(r"[(|,]", re.sub(r"\s+", " ", title))[0].strip()


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M")


def build_preview(text, wahl_keywords, enabled_opts, cfg, nach_keywords=()):
    """Alle Termine (Serien ausgeklappt, Berliner Lokalzeit) je Kalender als JSON-faehiges Dict, fuer den Builder."""
    chosen = resolve_wahl(wahl_keywords, cfg["wahlfach_pool"])
    cals = {c: [] for c in CALENDARS}
    nach = {}
    for kind, block in parse_items(text):
        if kind != "event":
            continue
        ev = parse_event(block)
        cal = assign(ev, chosen, enabled_opts, nach_keywords, cfg)
        title = re.sub(r"\s+KA-[A-Z0-9-]+.*$", "", re.sub(r"\s+", " ", ev["summary"]).strip())  # Kurslisten abschneiden
        if classify(ev, cfg) == "nachklausur" and selected(ev, chosen, enabled_opts, cfg):
            nach[nach_keyword(title)] = title
        times, rrule, exdates = {}, "", []
        for g in logical_lines(block):
            name = prop_name(g)
            if name in ("DTSTART", "DTEND") and name not in times:
                times[name] = (prop_value(g), "VALUE=DATE" in g[0].upper())
            elif name == "RRULE" and not rrule:
                rrule = prop_value(g)
            elif name == "EXDATE":
                exdates.append(prop_value(g))
        if "DTSTART" not in times:
            continue
        base = {"title": title, "location": ev["location"].strip()}
        start = icaltime.parse_value(times["DTSTART"][0])
        if start is None:  # ganztaegig
            day = icaltime.parse_date(times["DTSTART"][0])
            if day:
                cals[cal].append({**base, "start": day.strftime("%Y-%m-%d"), "end": day.strftime("%Y-%m-%d"), "allDay": True})
            continue
        end = icaltime.parse_value(times["DTEND"][0]) if "DTEND" in times else None
        length = end - start if end and end > start else timedelta(hours=1)
        starts = icaltime.expansions(rrule, icaltime.exdate_times(exdates), start) if rrule else [start]
        cals[cal] += [{**base, "start": iso(t), "end": iso(t + length), "allDay": False} for t in starts]
    for evs in cals.values():
        evs.sort(key=lambda e: (e["start"], e["title"]))
    return {
        "calendars": cals,
        "nachklausuren": [{"keyword": k, "title": t} for k, t in sorted(nach.items())],
    }


# --------------------------------------------------------------------------
# Upstream mit Cache (veraltete Daten, wenn Rapla gerade nicht antwortet)
# --------------------------------------------------------------------------
_cache = {}  # file -> (zeitpunkt, text); user steckt bewusst nicht im Schluessel: ein Kurs wird hoechstens alle cache_seconds geholt
_lock = threading.Lock()
_key_locks = {}
CFG = None


def fetch(user, file):
    key = file
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        key_lock = _key_locks.setdefault(key, threading.Lock())
    if hit and now - hit[0] < CFG["cache_seconds"]:
        return hit[1]
    with key_lock:  # gleichzeitige Anfragen auf denselben Kurs warten auf einen einzigen Abruf
        with _lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CFG["cache_seconds"]:
            return hit[1]
        url = f"{CFG['upstream']}?{urlencode({'user': user, 'file': file})}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rapla-ical-proxy/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(MAX_UPSTREAM_BYTES + 1)
            if len(raw) > MAX_UPSTREAM_BYTES:
                raise ValueError("Upstream-Antwort zu gross")
            text = raw.decode("utf-8")
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("Upstream lieferte keinen iCal-Feed")
        except Exception:
            if hit:  # veraltete Kopie ausliefern und Rapla erst nach retry_seconds wieder fragen
                with _lock:
                    _cache[key] = (time.time() - CFG["cache_seconds"] + CFG["retry_seconds"], hit[1])
                return hit[1]
            raise
        with _lock:
            if len(_cache) >= 256:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
            _cache[key] = (time.time(), text)
        return text


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
TRUTHY = {"1", "true", "yes", "ja", "on"}


class Handler(BaseHTTPRequestHandler):
    timeout = 15
    server_version = "rapla-proxy"

    def log_request(self, code="-", size="-"):  # ohne Query-String, damit der Token nicht im Log landet
        print(f"{self.address_string()} {self.command} {urlparse(self.path).path} {code}", flush=True)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", file=sys.stderr, flush=True)

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_HEAD = None  # unten gesetzt

    def _page(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                return self._send(200, f.read(), ctype)
        except OSError:
            return self._send(404, "nicht gefunden\n")

    def _params(self, q):
        """-> (user, file, wahl, optionals, nach); wirft ValueError mit Klartext fuer HTTP 400."""
        first = lambda k: (q.get(k) or [""])[0]
        user, file = first("user"), first("file")
        if not USER_RE.fullmatch(user) or not FILE_RE.fullmatch(file):
            raise ValueError("user und file muessen angegeben und gueltig sein")
        split = lambda key: [k.strip() for v in q.get(key, []) for k in v.split(",") if k.strip()]
        enabled = {k for k in CFG["optional"] if first(k).lower() in TRUTHY}
        wahl = split("wahl")
        resolve_wahl(wahl, CFG["wahlfach_pool"])  # Wahlfach-Fehler (400) vor dem Upstream-Abruf melden
        return user, file, wahl, enabled, split("nach")

    def do_GET(self):
        u = urlparse(self.path)
        if len(self.path) > 2000:
            return self._send(414, "URI zu lang\n")
        if u.path == "/health":
            return self._send(200, "ok\n")
        if u.path == "/":
            return self._send(302, "", extra={"Location": REPO_URL})
        if u.path in ("/builder", "/builder/"):
            return self._page("builder.html", "text/html; charset=utf-8")
        if u.path == "/builder/options.json":
            opts = {"wahlfach_pool": CFG["wahlfach_pool"], "optional": CFG["optional"], "calendars": CALENDARS}
            return self._send(200, json.dumps(opts, ensure_ascii=False), "application/json; charset=utf-8")
        m = re.fullmatch(r"/(vorlesungen|veranstaltungen|semester|klausuren|rest)\.ics", u.path)
        if not m and u.path != "/preview.json":
            return self._send(404, "nicht gefunden\n")
        try:
            user, file, wahl, enabled, nach = self._params(parse_qs(u.query))
        except ValueError as e:
            return self._send(400, f"{e}\n")
        try:
            text = fetch(user, file)
        except Exception as e:  # noqa: BLE001
            print(f"upstream-fehler: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return self._send(502, "Rapla nicht erreichbar\n")
        if not m:
            body = json.dumps(build_preview(text, wahl, enabled, CFG, nach), ensure_ascii=False)
            return self._send(200, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        cal = ALIASES.get(m.group(1), m.group(1))
        body = build_feed(text, cal, file, wahl, enabled, CFG, nach)
        self._send(200, body, "text/calendar; charset=utf-8", {"Cache-Control": "max-age=900"})


Handler.do_HEAD = Handler.do_GET


def main():
    global CFG
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.environ.get("CONFIG_FILE", "config.yaml"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--ics", help="Testmodus: lokale .ics-Datei filtern und ausgeben")
    ap.add_argument("--cal", choices=list(CALENDARS), default="vorlesungen")
    ap.add_argument("--wahl", default="")
    ap.add_argument("--ccna", action="store_true")
    ap.add_argument("--nach", default="", help="Nachklausur-Stichworte, mit Komma")
    args = ap.parse_args()
    CFG = load_config(args.config)

    if args.ics:
        with open(args.ics, encoding="utf-8") as f:
            text = f.read()
        enabled = set(CFG["optional"]) if args.ccna else set()
        sys.stdout.write(build_feed(text, args.cal, "TEST", args.wahl.split(","), enabled, CFG, args.nach.split(",")))
        return

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"lauscht auf {args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
