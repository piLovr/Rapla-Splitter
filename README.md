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
tar xzf rapla-proxy.tar.gz && cd rapla-proxy
cp .env.example .env
docker compose up -d --build  # alternativ: docker-compose up -d --build
```

Test auf dem Rechner, auf dem er läuft (das prüft auch, ob Rapla von dort erreichbar ist):

```bash
curl -s "http://127.0.0.1:8000/semester.ics?user=DEIN%40USER&file=25B6" | head
```

## Cloudflare Tunnel

Im Zero-Trust-Dashboard unter Networks → Tunnels einen Tunnel anlegen und einen Public Hostname
`cal.deinedomain.de` auf Service `http://rapla-proxy:8000` setzen.

- **Neuer Tunnel:** Token in `.env` als `TUNNEL_TOKEN` eintragen und mit
  `docker compose --profile tunnel up -d` starten.
- **Vorhandener Tunnel (läuft schon):** Hostname dort ergänzen. Läuft `cloudflared` in einem anderen
  Docker-Netz, nimm als Service `http://<IP-des-Hosts>:8000` (der Port ist an `127.0.0.1` gebunden,
  dann musst du in `docker-compose.yml` die Bindung auf `0.0.0.0` oder das Docker-Gateway ändern).
- **Kein Cloudflare Access davorschalten**, SOGo/Nextcloud können sich dort nicht anmelden. Der Proxy hat keine
  Zugriffskontrolle (nur Kurs und Rapla-User in der URL). Falls ein Abo-Abruf blockiert wird: in Cloudflare unter Security → WAF
  eine Skip-Regel für den Hostnamen anlegen, Bot-Schutz kann Kalender-Clients abweisen.

## Neues Semester / neue Wahlfächer

`config.yaml` öffnen, unter `wahlfach_pool` die neuen Fächer eintragen (Teil des Rapla-Titels),
dann `docker compose restart rapla-proxy`. Weitere optionale Fächer wie CCNA gehören unter `optional`
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
