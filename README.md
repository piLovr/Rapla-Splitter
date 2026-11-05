# Rapla iCal Filter Proxy

Teilt den Rapla-Feed (DHBW) in vier Kalender auf und filtert dein Wahlfach heraus.

| URL | Inhalt |
|---|---|
| `/vorlesungen.ics` | Lehrveranstaltungen (graue Termine) |
| `/semester.ics` | alles Übrige (Infoveranstaltungen, Semesterstart-Besprechung …) |
| `/klausuren.ics` | Prüfungen, außer "nur geladene" |
| `/nachklausuren.ics` | Prüfungen für Geladene |

Feiertage und die Pseudo-Ganztägigen ("Beginn Theorie", "Klausurwoche") fehlen überall; sie stehen unter
`ignore` in `config.yaml`. Alles andere, was weder Vorlesung noch Prüfung ist, landet in `semester.ics`.
Römische Zahlen gelten als arabische (`Webengineering 3` = `Webengineering III`).

## Parameter

| Parameter | Pflicht | Bedeutung |
|---|---|---|
| `user` | ja | Rapla-User, wird an Rapla durchgereicht (`@` darf als `%40` kodiert sein) |
| `file` | ja | Rapla-Kurs, z. B. `25B6` |
| `wahl` | nein | Stichwort deines Wahlfachs, z. B. `Signale`, `Währung`, `Web`, `Geschäft`, `Linux`, `Robotik`, `Workflow`, `OO` |
| `ccna` | nein | `ccna=1` nimmt CCNA (Vorlesung + Klausur) mit auf, sonst fehlt es |

Beispiel:

```
https://cal.deinedomain.de/vorlesungen.ics?user=DEIN%40USER&file=25B6&wahl=Signale
https://cal.deinedomain.de/klausuren.ics?user=DEIN%40USER&file=25B6&wahl=Signale
```

Das Stichwort muss genau ein Wahlfach aus `wahlfach_pool` treffen. Sonst gibt es einen Fehler (HTTP 400)
mit der Liste der gültigen Fächer. Groß-/Kleinschreibung, Leerzeichen und Umlaute spielen keine Rolle.

## Installation

```bash
docker compose up -d   # zieht ghcr.io/pilovr/rapla-splitter:latest (wird bei jedem Push auf main gebaut)
```

Test auf dem Rechner, auf dem er läuft (das prüft auch, ob Rapla von dort erreichbar ist):

```bash
curl -s "http://127.0.0.1:8000/semester.ics?user=DEIN%40USER&file=25B6" | head
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

- Rapla wird höchstens alle 15 Minuten abgefragt. Ist Rapla nicht erreichbar, kommt die letzte gute Kopie.
- Termine werden unverändert durchgereicht (Serien mit `RRULE`/`EXDATE`, Zeitzonen). Einzige Korrektur:
  das doppelte `DTSTAMP` in jedem Rapla-Termin wird entfernt.
- Ein `token=`-Parameter (alte URLs) wird ignoriert; der Query-String landet nicht im Zugriffslog.

## Tests

```bash
pip install pyyaml
RAPLA_ICS=/pfad/zur/25B6.ics python3 test_proxy.py
python3 app.py --ics /pfad/zur/25B6.ics --cal klausuren --wahl Signale   # Ausgabe ansehen
```
