"""Zeit-Helfer fuer iCal ohne tzdata (Slim-Images haben keine): Europe/Berlin und einfache Serientermine."""
import re
from datetime import datetime, timedelta


def _last_sunday(year, month):
    d = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _summer(utc):
    """Sommerzeit in Deutschland: letzter So. Maerz bis letzter So. Oktober, jeweils 01:00 UTC."""
    return _last_sunday(utc.year, 3).replace(hour=1) <= utc < _last_sunday(utc.year, 10).replace(hour=1)


def berlin_to_utc(local):
    return local - timedelta(hours=2 if _summer(local - timedelta(hours=1)) else 1)


def utc_to_berlin(utc):
    return utc + timedelta(hours=2 if _summer(utc) else 1)


def parse_value(value):
    """iCal-Zeitwert -> lokale (Berliner) Zeit; None fuer ganztaegige Termine (VALUE=DATE)."""
    m = re.search(r"(\d{8}T\d{6})(Z?)\s*$", value)
    if not m:
        return None
    t = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    return utc_to_berlin(t) if m.group(2) else t


def parse_date(value):
    m = re.search(r"(\d{8})\s*$", value)
    return datetime.strptime(m.group(1), "%Y%m%d") if m else None


def exdate_times(values):
    """values: Werte der EXDATE-Zeilen (je kommagetrennt) -> Menge lokaler Zeiten."""
    out = set()
    for value in values:
        for v in value.split(","):
            t = parse_value(v.strip())
            if t:
                out.add(t)
    return out


def expansions(rrule, exdates, start):
    """Wiederholungen eines Rapla-Serientermins (FREQ=DAILY/WEEKLY mit COUNT/UNTIL/INTERVAL; Rapla nutzt nur das)
    -> Liste lokaler Startzeiten ohne EXDATE-Ausnahmen."""
    rule = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    step = {"DAILY": 1, "WEEKLY": 7}.get(rule.get("FREQ"))
    if not step:
        return [start]
    step *= int(rule.get("INTERVAL", 1))
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        until = utc_to_berlin(datetime.strptime(rule["UNTIL"][:15].ljust(15, "0")[:15], "%Y%m%dT%H%M%S"))
    out, i = [], 0
    while (count is None or i < count) and i < 1000:
        t = start + timedelta(days=step * i)
        if until and t > until:
            break
        if t not in exdates:
            out.append(t)
        i += 1
    return out
