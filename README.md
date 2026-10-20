# SimHeizstabEVCC – Heizstab-Steuerung für evcc

Heizstab wird als steuerbarer Verbraucher für evcc simuliert, real aber stufenweise geschaltet.

Steuert einen 3-stufigen Heizstab (3 × 1 kW) über einen Tasmota-ESP mit Relais und bindet ihn
als **Custom Charger** in [evcc](https://evcc.io) ein. evcc gibt den PV-Überschuss als Strom
(Ampere) vor, das Skript wählt daraus die passende Heizstufe und schaltet die Relais.

> **Sicherheit:** Der Heizstab muss immer einen eigenen Thermostat bzw. Sicherheits-
> temperaturbegrenzer haben. Diese Software (und auch das Temperaturlimit in evcc) ist
> **keine** Sicherheitsfunktion.

## Funktionsweise

```
evcc ──HTTP──> control.py ──HTTP (Tasmota)──> ESP-01 ──> Relais ──> Heizstab
                   │
                   ├─> Home Assistant (Temperatur)
                   ├─> NAS (Logdatei per FTPS/SMB)
                   └─> Weboberfläche  http://<pi>:5000/config
```

1. evcc gibt den Heizstab frei (`/enable`) und schickt den Sollstrom (`/setcurrent`).
2. Sollleistung = Ampere × Spannung × Phasen (wie evcc rechnet, z. B. 2 A × 230 V × 3 = 1.380 W).
3. Daraus wird die Stufe bestimmt – **mit Hysterese**:
   | Stufe | Relais (Standard) | Heizleistung | einschalten ab | ausschalten unter |
   |---|---|---|---|---|
   | 1 | 4 | 1.000 W | 1.200 W | 1.000 W |
   | 2 | 3, 4 | 2.000 W | 2.200 W | 2.000 W |
   | 3 | 2, 3, 4 | 3.000 W | 3.200 W | 3.000 W |

   Einschalten ab = Stufe × Leistung pro Stufe + Puffer.
4. Verzögerungen und Mindestlaufzeit verhindern häufiges Schalten.
5. `/status` meldet evcc die **tatsächlich** am ESP geschaltete Leistung und die Temperatur.

### Schutzfunktionen
- **evcc sperrt** → sofort aus (ohne Verzögerung).
- **Watchdog:** kein Kontakt zu evcc für `evcc_timeout_seconds` → aus.
- **Soll/Ist-Abgleich:** weicht der Relaiszustand 3× in Folge ab, werden die Relais neu gesendet.
- **ESP nicht erreichbar:** evcc bekommt 0 W gemeldet.
- Alle Relais werden mit **einem** Tasmota-`Backlog`-Befehl geschaltet, mit Wiederholungen bei
  schlechtem WLAN. Nur die in den Stufen verwendeten Relais werden angefasst.
- Freigabe und Sollstrom überleben einen Neustart (`evcc_state.json`).

## Dateien

| Datei | Inhalt |
|---|---|
| `Install/control.py` | Steuerung, HTTP-Schnittstelle f?r evcc, Logging |
| `Install/webui.py` | Weboberfl?che zum Bearbeiten der Konfiguration |
| `Install/config.example.json` | Vorlage f?r `config.json` |
| `Install/heizstab.service` | systemd-Dienst f?r den Autostart |
| `evcc.example.yaml` | Charger- und Ladepunkt-Ausschnitt f?r die `evcc.yaml` |
| `tests/` | Tests (laufen ohne echte Hardware) |

Der Ordner `Install/` enth?lt genau die Dateien, die auf den Pi geh?ren.
`config.json` (mit Passwörtern), Logdateien und `evcc_state.json` sind bewusst **nicht** im
Repository.

## Installation (Raspberry Pi)

Das Programm l?uft direkt aus dem geklonten Repository. `config.json`, `config.json.bak`
und `evcc_state.json` liegen zwar in `Install/`, sind aber nicht im Repository ? `git pull`
?berschreibt sie daher nie.

### Erstinstallation

```bash
sudo apt install git python3-flask python3-requests
cd ~
git clone https://github.com/Booman2-prog/SimHeizstabEVCC.git
cd ~/SimHeizstabEVCC/Install
cp config.example.json config.json      # anpassen, oder sp?ter ?ber die Weboberfl?che
sudo cp heizstab.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heizstab
systemctl status heizstab               # muss "active (running)" zeigen
```

Der Dienst geht von Benutzer `homeserver` und Ordner `/home/homeserver/SimHeizstabEVCC` aus ?
bei Bedarf in `heizstab.service` anpassen.

### Update

```bash
cd ~/SimHeizstabEVCC
git pull
sudo systemctl restart heizstab
```

Nur falls sich `Install/heizstab.service` ge?ndert hat, zus?tzlich vor dem Neustart:

```bash
sudo cp Install/heizstab.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

### Bedienung

| Aufgabe | Befehl |
|---|---|
| Live-Ausgabe | `journalctl -u heizstab -f` |
| Neu starten | `sudo systemctl restart heizstab` |
| Stoppen | `sudo systemctl stop heizstab` |

Nicht zus?tzlich von Hand `python control.py` starten ? sonst ist Port 5000 doppelt belegt.

## evcc einrichten

Den Ausschnitt aus [`evcc.example.yaml`](evcc.example.yaml) in die `evcc.yaml` übernehmen und
evcc neu starten (`sudo systemctl restart evcc`). Wichtig:

- Platzhalter sind `{{.enable}}`, `{{.maxcurrent}}` und `{{.maxcurrentmillis}}`.
- `maxcurrentmillis` (optional) überträgt Kommawerte. Damit nutzt der Heizstab den Überschuss
  an den Umschaltpunkten besser (1 A = 690 W bei 3 Phasen; ohne Kommawerte bleiben bis zu
  690 W ungenutzt).
- `mincurrent` so wählen, dass Stufe 1 auch wirklich einschaltet (Standard: ≥ 1,74 A, also 2
  bzw. 1.8). Die Weboberfläche zeigt den genauen Wert an.
- Features `heating` + `integrateddevice` (kein `continuous` – das ist für Wärmepumpen, die
  im gesperrten Zustand selbst weiterlaufen).

## Weboberfläche

`http://<pi>:5000/config`

- Alle Einstellungen gruppiert, mit Prüfung (Zahlenbereiche, Relais 1–8, Abschalt- unter
  Einschaltschwelle). Ungültiges wird nicht gespeichert.
- **Leistungsberechnung:** zeigt live, was evcc meldet, die Umrechnung in Watt, Ziel- und
  aktuelle Stufe, alle Schwellen und *warum* gerade (nicht) geschaltet wird. Mit Rechner
  „Was wäre bei … A?".
- Änderungen wirken sofort (kein Neustart). Vorige Version bleibt als `config.json.bak`.
- Passwörter/Tokens werden nie angezeigt; leeres Feld = unverändert.
- Schutz per Benutzer/Passwort (`web_benutzer`/`web_passwort`) – **beim ersten Aufruf ein
  Passwort setzen**, sonst kann jeder im Netzwerk die Einstellungen ändern.

## Konfiguration (`config.json`)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `esp_heizstab_ip` | – | IP des Tasmota-ESP |
| `esp_user` / `esp_password` | `admin` / – | Tasmota-Weblogin (leer = ohne Login) |
| `esp_timeout` / `esp_versuche` | 3 / 3 | Timeout (s) und Versuche pro Befehl |
| `relais_stufe_1..3` | `[4]`, `[3,4]`, `[2,3,4]` | Relais je Stufe |
| `leistung_pro_stufe` | 1000 | W pro Stufe |
| `spannung` / `phasen` | 230 / 3 | Umrechnung Ampere → Watt |
| `puffer_einschalten` | 200 | W über der Stufenleistung zum Einschalten |
| `abschalt_unter_s1..3` | 1000/2000/3000 | Abschaltschwellen in W |
| `zeit_bis_zuschalten` | 60 | s Verzögerung beim Hochschalten |
| `zeit_bis_abschalten` | 10 | s Verzögerung beim Runterschalten |
| `mindest_laufzeit` | 300 | s Mindestlaufzeit einer Stufe |
| `evcc_timeout_seconds` | 300 | Watchdog |
| `use_home_assistant` | true | Temperatur aus HA, sonst Tasmota THR316D |
| `ha_url`, `ha_token`, `ha_sensor_entity`, `ha_timeout` | | Home Assistant |
| `tasmota_thr316d_ip` | | Alternative Temperaturquelle |
| `log_level` | INFO | `DEBUG` loggt jede evcc-Anfrage |
| `log_max_mb` / `log_anzahl_dateien` | 1 / 3 | Größenbegrenzung der Logdatei |
| `log_nas.*` | | siehe Logging |
| `web_benutzer` / `web_passwort` | `admin` / leer | Zugang Weboberfläche |

## Logging

Das Skript schreibt die Logdatei **direkt aufs NAS** – ohne Mount, ohne root:

```json
"log_nas": {
    "protokoll": "ftps",          // oder "smb"
    "server": "10.10.10.2",
    "port": 21,
    "ordner": "/NAS-Nacht/EVCC-Logging",
    "datei": "heizstab.log",
    "benutzer": "...",
    "passwort": "...",
    "neuer_versuch_sekunden": 300
}
```

- **FTPS** (FTP mit TLS) braucht keine Zusatzpakete. **SMB** braucht das pip-Paket
  `smbprotocol` und zusätzlich `"freigabe"`.
- NAS nicht erreichbar → Steuerung läuft **ohne Logdatei** weiter, neuer Versuch nach
  `neuer_versuch_sekunden`. Es wird nichts lokal auf die SD-Karte geschrieben.
- Geschrieben wird in einem eigenen Thread – ein langsames NAS bremst die Steuerung nie.
- Rotation: max. `log_max_mb` pro Datei, `log_anzahl_dateien` alte Dateien.
- Im Log stehen Stufenwechsel, Freigaben, Warnungen und Fehler. Konsole bzw.
  `journalctl -u heizstab` zeigen zusätzlich den Zustand des NAS-Loggings.

Tipp: auf dem NAS einen eigenen Benutzer anlegen, der nur in den Log-Ordner schreiben darf.

## HTTP-Schnittstelle

| Endpunkt | Aufrufer | Antwort |
|---|---|---|
| `GET /status` | evcc | `{"status": "B"/"C", "enabled", "power", "stage", "esp_online", "temp"}` |
| `GET /enable?enable=true\|false` | evcc | Freigabe setzen |
| `GET /setcurrent?ampere=<A>` | evcc | Sollstrom (auch Kommawerte) |
| `GET /config` | Browser | Weboberfläche |
| `GET /config/rechnung[?ampere=<A>]` | Browser | Leistungsberechnung als JSON |

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests
```

Die Tests simulieren ESP, NAS (FTPS/SMB) und Konfiguration – keine Hardware nötig, die echte
`config.json` wird nicht verändert.

## Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| evcc zeigt 0 W, Relais schalten nicht | `journalctl -u heizstab -f`: kommt `/setcurrent` an? Platzhalter `{{.maxcurrent}}` prüfen |
| `401 Nicht autorisiert` vom ESP | `esp_user`/`esp_password` setzen |
| ESP-Timeouts | schwaches WLAN; in der Tasmota-Konsole `Sleep 0`, ESP/Router besser platzieren |
| „Port 5000 belegt" | Programm läuft schon (Dienst) – nicht zusätzlich von Hand starten |
| evcc „aktiv", aber 0 W | `mincurrent` zu klein (unter Einschaltschwelle Stufe 1) |
| Keine Logdatei auf dem NAS | Konsole zeigt den Grund; Zugangsdaten, IP, Ordner prüfen |

## Lizenz und Haftung

[MIT-Lizenz](LICENSE) ? frei nutzbar, ver?nderbar und weitergebbar, solange der Lizenzhinweis
erhalten bleibt.

Die Software wird ohne Gew?hr bereitgestellt. Sie schaltet elektrische Lasten (230 V) ?
Installation und Anschluss des Heizstabs geh?ren in die H?nde einer Elektrofachkraft, und der
Heizstab muss ?ber einen eigenen Sicherheitstemperaturbegrenzer verf?gen. Nutzung auf eigene
Verantwortung.
