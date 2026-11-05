# Rapla iCal Filter Proxy

Teilt den Rapla-Feed (DHBW) in vier Kalender auf, die sich **nicht überschneiden** und zusammen **alles** enthalten,
was im originalen Rapla steht.

| URL | Inhalt | Farbe im Builder |
|---|---|---|
| `/vorlesungen.ics` | Lehrveranstaltungen deiner Fächer | gelb |
| `/veranstaltungen.ics` | alles Übrige (Semesterstart-Besprechung, Infoveranstaltungen …); der alte Pfad `/semester.ics` geht weiter | blau |
| `/klausuren.ics` | Prüfungen deiner Fächer, dazu die Nachklausuren, die du schreiben musst (`nach=`) | rot |
| `/rest.ics` | alles, was in keinem der drei anderen liegt: Feiertage, "Beginn Theorie"/"Klausurwoche", nicht belegte Wahlfächer samt Klausuren, nicht gewählte Optionals, Nachklausuren, die du nicht schreibst | grau |

Der Kalender `/nachklausuren.ics` gibt es nicht mehr: Nachklausuren stehen nur in `klausuren.ics`, wenn du sie
per `nach=` auswählst, sonst in `rest.ics`.

Die Basis-URL ohne Pfad leitet auf dieses Repo weiter. **Unter `/builder` gibt es einen grafischen URL-Builder**
mit Live-Vorschau aller vier Kalender in einer Wochenansicht.

## Parameter

Alle vier Kalender verstehen dieselben Parameter:

| Parameter | Pflicht | Bedeutung |
|---|---|---|
| `user` | ja | Rapla-User, wird an Rapla durchgereicht (`@` darf als `%40` kodiert sein) |
| `file` | ja | Rapla-Kurs, z. B. `25B6` |
| `wahl` | nein | Stichwort deines Wahlfachs, z. B. `Signale`, `Währung`, `Web`, `Geschäft`, `Linux`, `Robotik`, `Workflow`, `OO`; mehrere mit Komma |
| `ccna` | nein | `ccna=1` nimmt CCNA (Vorlesung + Klausur) mit auf, sonst liegt es in `rest.ics` |
| `nach` | nein | Stichworte der Nachklausuren, die du schreiben musst, mehrere mit Komma; `nach=alle` nimmt alle |
| `token` | nein | wird ignoriert, alte URLs mit Token funktionieren weiter |

Beispiel:

```
https://cal.deinedomain.de/vorlesungen.ics?user=DEIN%40USER&file=25B6&wahl=Signale
https://cal.deinedomain.de/klausuren.ics?user=DEIN%40USER&file=25B6&wahl=Signale&nach=Statistik
```

Welche Nachklausuren es gibt (und das passende Stichwort), zeigt der Builder; sie gelten als Nachklausur, wenn der
Titel etwas aus `nachklausur_match` in `config.yaml` enthält (`geladene`, `Wdh`, `Wiederholung` …).

Das Stichwort muss genau ein Wahlfach aus `wahlfach_pool` treffen. Sonst gibt es einen Fehler (HTTP 400)
mit der Liste der gültigen Fächer. Groß-/Kleinschreibung, Leerzeichen und Umlaute spielen keine Rolle, römische Zahlen gelten als arabische
(`Webengineering 3` = `Webengineering III`) und einzelne Tippfehler in Rapla-Titeln werden toleriert.
Termine, die in keinem Kalender stehen sollen (Feiertage …), stehen unter `ignore` in `config.yaml` und landen in `rest.ics`.

## Installation

```bash
docker compose up -d   # zieht ghcr.io/pilovr/rapla-splitter:latest (wird bei jedem Push auf main gebaut)
```

Test auf dem Rechner, auf dem er läuft (das prüft auch, ob Rapla von dort erreichbar ist):

```bash
curl -s "http://127.0.0.1:8000/veranstaltungen.ics?user=DEIN%40USER&file=25B6" | head
```

## Cloudflare Tunnel

Der Tunnel ist nicht Teil dieses Projekts. Der Container hängt im Docker-Netz deines `cloudflared`
(in `docker-compose.yml` unter `networks` den Netznamen anpassen, `docker network ls`). Im Zero-Trust-Dashboard
einen Public Hostname `cal.deinedomain.de` auf Service `http://rapla-proxy:8000` setzen.

- **Kein Cloudflare Access davorschalten**, SOGo/Nextcloud können sich dort nicht anmelden. Der Proxy hat keine
  Zugriffskontrolle (nur Kurs und Rapla-User in der URL). Falls ein Abo-Abruf blockiert wird: in Cloudflare unter Security → WAF
  eine Skip-Regel für den Hostnamen anlegen, Bot-Schutz kann Kalender-Clients abweisen.

## Portainer

Stack mit der `docker-compose.yml` anlegen (Image wird von GHCR gezogen, nach neuem Push "Pull and redeploy").
Die `config.yaml` ist im Image eingebaut; für eigene Werte die Volume-Zeile in der Compose-Datei aktivieren.

## Neues Semester / neue Wahlfächer

`config.yaml` öffnen, unter `wahlfach_pool` die neuen Fächer eintragen (Teil des Rapla-Titels),
dann pushen (Image wird neu gebaut) oder bei gemounteter Datei `docker compose restart rapla-proxy`. Weitere optionale Fächer wie CCNA gehören unter `optional`
(`schluessel: Titelteil`, aktiv per `?schluessel=1`).

## Verhalten

- Rapla wird pro Kurs höchstens alle 12 Stunden abgefragt (`cache_seconds` in `config.yaml`), egal wie viele URLs, Wahlfächer
  oder Builder-Vorschauen darauf zugreifen; gleichzeitige Anfragen teilen sich einen Abruf. Der Cache liegt im Arbeitsspeicher
  (ein Container-Neustart leert ihn). Ist Rapla nicht erreichbar, kommt die letzte gute Kopie, und Rapla wird erst nach
  `retry_seconds` (15 min) erneut gefragt.
- Termine werden unverändert durchgereicht (Serien mit `RRULE`/`EXDATE`, Zeitzonen). Einzige Korrektur:
  das doppelte `DTSTAMP` in jedem Rapla-Termin wird entfernt.
- Ein `token=`-Parameter (alte URLs) wird ignoriert; der Query-String landet nicht im Zugriffslog.
- Schutz vor Missbrauch (`rate_limit` in `config.yaml`, pro Client-IP, im Arbeitsspeicher): höchstens 240 Anfragen pro Minute
  (sonst `429`), höchstens 20 echte Rapla-Abrufe pro 10 Minuten (Cache-Treffer zählen nicht); wer das reißt, ist 30 Minuten
  gesperrt. Hinter dem Cloudflare Tunnel wird die IP aus `CF-Connecting-IP` gelesen (`client_ip_header`).
- Unbekannte Kurse/User bei Rapla ergeben `404` mit Rapla-Meldung (10 Minuten zwischengespeichert, also keine Abruf-Flut durch
  Tippfehler). Der Builder lädt nur auf Knopfdruck, nicht beim Tippen.

## Wahlfächer aller Studiengänge finden

`wahlfaecher.py` lädt (wie der Roomfinder) die Kursliste von `api.dhbw.app` und jeden Stundenplan von `dhbw.app`
(eine Anfrage nach der anderen, 150 ms Pause). Ein Wahlfach erkennt es daran, dass im selben Kurs mehrere
verschiedene Veranstaltungen zur selben Zeit liegen (mindestens zweimal und bei mindestens der Hälfte der Termine),
man also nur eine davon besucht. Prüfungen, Feiertage, Besprechungen und parallele Gruppen (`Gruppe A/B`) zählen nicht.

```bash
python3 wahlfaecher.py --since 2026-11-05 --cache ics-cache --json wahlfaecher.json
python3 wahlfaecher.py --ics 25B6.ics      # lokale Datei (dhbw.app- oder Rapla-Export), kein Netz
```

Ausgabe: Wahlfächer je Studiengang (mit den Kursen, in denen sie vorkommen) und ein fertiger `wahlfaecher:`-Block
je Studiengang. `--since` blendet abgeschlossene Jahrgänge aus, `--cache` speichert die Pläne zwischen.
Die Liste ist eine Heuristik: Namen aus verschiedenen Studiengängen nicht in einen gemeinsamen `wahlfach_pool`
kippen (ein Eintrag `Informatik` würde auch `Technische Informatik` erwischen).

## Tests

```bash
pip install pyyaml
RAPLA_ICS=/pfad/zur/25B6.ics python3 test_proxy.py
python3 app.py --ics /pfad/zur/25B6.ics --cal klausuren --wahl Signale   # Ausgabe ansehen
python3 test_wahlfaecher.py                                              # Wahlfach-Scraper (offline)
```
