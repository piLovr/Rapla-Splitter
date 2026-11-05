#!/usr/bin/env python3
"""Rapla iCal filter proxy.

Holt den iCal-Feed von Rapla (DHBW) und teilt ihn in vier Kalender auf:
  /vorlesungen.ics    Lehrveranstaltungen
  /semester.ics       alles Uebrige (Infoveranstaltungen, Semesterstart ...), ausser Feiertage
                      & Co. (config.yaml: ignore)
  /klausuren.ics      Pruefungen (ohne "geladene")
  /nachklausuren.ics  Pruefungen fuer Geladene

Query-Parameter:
  token   optional, wird ignoriert (alte URLs mit Token funktionieren weiter)
  user    Pflicht, Rapla-User (wird 1:1 an Rapla durchgereicht)
  file    Pflicht, Rapla-Kurs (z. B. 25B6)
  wahl    Stichwort des gewaehlten Wahlfachs, z. B. wahl=Signale
  ccna    ccna=1 nimmt optionale Faecher mit auf (Schluessel stehen in config.yaml)
"""
import argparse
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import yaml

MAX_UPSTREAM_BYTES = 20 * 1024 * 1024
USER_RE = re.compile(r"[A-Za-z0-9._@+\-]{1,80}")
FILE_RE = re.compile(r"[A-Za-z0-9_\-]{1,40}")
CALENDARS = {
    "vorlesungen": "Vorlesungen",
    "semester": "Semester",
    "klausuren": "Klausuren",
    "nachklausuren": "Nachklausuren",
}


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("cache_seconds", 900)
    cfg.setdefault("wahlfach_pool", [])
    cfg.setdefault("optional", {})
    cfg.setdefault("ignore", [])
    cfg["categories"] = {
        "lecture": "Lehrveranstaltung",
        "exam": "Prüfung",
        "other": "Sonstiger Termin",
        **(cfg.get("categories") or {}),
    }
    cfg.setdefault("nachklausur_match", "geladene")
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
    cats = cfg["categories"]
    if cats["lecture"].casefold() in ev["categories"]:
        return "vorlesungen"
    if cats["exam"].casefold() in ev["categories"]:
        if norm(cfg["nachklausur_match"]) in norm(ev["summary"]):
            return "nachklausuren"
        return "klausuren"
    title = norm(ev["summary"])
    if any(norm(p) in title for p in cfg["ignore"]):
        return None  # Feiertage, "Beginn Theorie", "Klausurwoche" -> raus
    return "semester"  # alles Uebrige, damit kein Termin durchrutscht


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


def selected(ev, chosen, enabled_opts, cfg):
    title = norm(ev["summary"])
    if chosen:
        matched = {e for e in cfg["wahlfach_pool"] if norm(e) in title}
        if matched and not (matched & chosen):
            return False
    for key, pattern in cfg["optional"].items():
        if norm(pattern) in title and key not in enabled_opts:
            return False
    return True


def build_feed(text, cal, file_label, wahl_keywords, enabled_opts, cfg):
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
        if classify(ev, cfg) != cal or not selected(ev, chosen, enabled_opts, cfg):
            continue
        out.extend(dedupe_dtstamp(data))
    return "".join(out)


# --------------------------------------------------------------------------
# Upstream mit Cache (veraltete Daten, wenn Rapla gerade nicht antwortet)
# --------------------------------------------------------------------------
_cache = {}
_lock = threading.Lock()
CFG = None


def fetch(user, file):
    key = (user, file)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CFG["cache_seconds"]:
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
        if hit:
            return hit[1]
        raise
    with _lock:
        if len(_cache) >= 64:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, text)
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

    def do_GET(self):
        u = urlparse(self.path)
        if len(self.path) > 2000:
            return self._send(414, "URI zu lang\n")
        if u.path == "/health":
            return self._send(200, "ok\n")
        m = re.fullmatch(r"/(vorlesungen|semester|klausuren|nachklausuren)\.ics", u.path)
        if not m:
            return self._send(404, "nicht gefunden\n")
        q = parse_qs(u.query)
        first = lambda k: (q.get(k) or [""])[0]
        user, file = first("user"), first("file")
        if not USER_RE.fullmatch(user) or not FILE_RE.fullmatch(file):
            return self._send(400, "user und file muessen angegeben und gueltig sein\n")
        keywords = [k for v in q.get("wahl", []) for k in v.split(",")]
        enabled = {k for k in CFG["optional"] if first(k).lower() in TRUTHY}
        try:
            # Wahlfach-Fehler (400) vor dem Upstream-Abruf melden
            resolve_wahl(keywords, CFG["wahlfach_pool"])
        except ValueError as e:
            return self._send(400, f"{e}\n")
        try:
            text = fetch(user, file)
        except Exception as e:  # noqa: BLE001
            print(f"upstream-fehler: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return self._send(502, "Rapla nicht erreichbar\n")
        body = build_feed(text, m.group(1), file, keywords, enabled, CFG)
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
    args = ap.parse_args()
    CFG = load_config(args.config)

    if args.ics:
        with open(args.ics, encoding="utf-8") as f:
            text = f.read()
        enabled = set(CFG["optional"]) if args.ccna else set()
        sys.stdout.write(build_feed(text, args.cal, "TEST", args.wahl.split(","), enabled, CFG))
        return

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"lauscht auf {args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
