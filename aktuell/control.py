"""
Heizstab-Steuerung fuer evcc (Custom Charger).

Endpunkte (passend zur evcc.yaml):
  GET /status                 -> {"status", "enabled", "power", "temp"}
  GET /enable?enable=true|false
  GET /setcurrent?ampere=<A>
"""
from flask import Flask, request, jsonify
from logging.handlers import QueueHandler, QueueListener
import ftplib
import io
import logging
import queue
import requests
import threading
import time
import json
import math
import os
import sys

try:  # Direkter SMB-Zugriff aufs NAS (ohne mount, ohne root): pip-Paket "smbprotocol"
    import smbclient
    SMB_IMPORT_ERROR = None
except ImportError as e:
    smbclient = None
    SMB_IMPORT_ERROR = e

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# config.json immer neben diesem Skript suchen (unabhaengig vom Startverzeichnis)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

logger = logging.getLogger("heizstab")
logger.setLevel(logging.DEBUG)
logger.propagate = False
# Faellt die Logdatei waehrend des Betriebs weg (z. B. NAS getrennt), laeuft die
# Steuerung weiter, ohne bei jeder Zeile einen Traceback auszugeben.
logging.raiseExceptions = False

lock = threading.RLock()

state = {
    "enabled": False,          # Freigabe durch evcc (/enable)
    "ampere": 0.0,             # zuletzt von evcc vorgegebener Strom (/setcurrent)
    "current_stage": 0,        # aktuell geschaltete Stufe (Soll an den ESP)
    "target_stage": 0,         # Stufe, auf die gerade gewartet wird
    "target_since": 0.0,       # seit wann target_stage ansteht
    "last_switch": 0.0,        # Zeitpunkt des letzten Stufenwechsels
    "last_evcc_contact": time.time(),
    "mismatch_counter": 0,
    "esp_cache": None,         # (zeit, actual_stage, online)
    "temp_cache": None,        # (zeit, temp)
}

ESP_CACHE_SECONDS = 2
TEMP_CACHE_SECONDS = 30

# Letzte Vorgaben von evcc (Freigabe + Strom) ueberleben einen Neustart dieses Skripts.
# evcc sendet den Strom nur bei Aenderung erneut - ohne Speicherung waere er nach
# einem Neustart verloren.
STATE_FILE = os.path.join(BASE_DIR, "evcc_state.json")

DEBUG, INFO, WARNUNG, FEHLER = logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR


def log(msg, level=INFO):
    logger.log(level, msg)


def console_note(msg):
    """Meldung nur auf die Konsole (z. B. ueber den Zustand des NAS-Loggings selbst)."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} LOGGING {msg}", flush=True)


class NasLogHandler(logging.Handler):
    """Schreibt Logzeilen direkt in eine Datei auf dem NAS, mit Groessenbegrenzung.

    - Verbindet sich selbststaendig (kein mount, kein root noetig).
    - Ist das NAS nicht erreichbar, werden Logzeilen verworfen - die Steuerung laeuft
      unveraendert weiter. Alle `retry` Sekunden wird ein neuer Verbindungsversuch gemacht.
    - Laeuft ueber einen QueueListener in einem eigenen Thread, damit ein langsames NAS
      die Heizstab-Steuerung nie blockiert.

    Unterklassen implementieren nur den Dateizugriff (_connect, _size, _move, _delete,
    _append, _disconnect).
    """

    def __init__(self, path, max_bytes, backups, retry):
        super().__init__()
        self.path = path
        self.max_bytes, self.backups, self.retry = max_bytes, backups, retry
        self.online = False
        self.next_try = 0.0
        self.size = 0

    # --- vom Protokoll abhaengig ---
    def _connect(self): raise NotImplementedError
    def _size(self, path): raise NotImplementedError        # OSError/Exception wenn fehlt
    def _move(self, src, dst): raise NotImplementedError    # dst wird ueberschrieben
    def _delete(self, path): raise NotImplementedError
    def _append(self, path, data): raise NotImplementedError
    def _disconnect(self): pass

    # --- gemeinsame Logik ---
    def _open(self):
        self._connect()
        try:
            self.size = self._size(self.path)
        except Exception:
            self.size = 0

    def _rotate(self):
        for i in range(self.backups - 1, 0, -1):
            try:
                self._move(f"{self.path}.{i}", f"{self.path}.{i + 1}")
            except Exception:
                pass  # aeltere Datei existiert (noch) nicht
        if self.backups > 0:
            self._move(self.path, f"{self.path}.1")
        else:
            self._delete(self.path)
        self.size = 0

    def _write(self, line):
        if self.size + len(line) > self.max_bytes:
            self._rotate()
        self._append(self.path, line)
        self.size += len(line)

    def _go_offline(self, reason):
        if self.online or self.next_try == 0.0:
            console_note(f"NAS {self.path} nicht erreichbar ({reason}) - laufe ohne Logdatei, "
                         f"neuer Versuch in {self.retry:.0f}s")
        self.online = False
        self.next_try = time.time() + self.retry
        try:
            self._disconnect()
        except Exception:
            pass

    def emit(self, record):
        if not self.online:
            if time.time() < self.next_try:
                return  # NAS offline: Zeile verwerfen
            try:
                self._open()
            except Exception as e:
                self._go_offline(e)
                return
            self.online = True
            console_note(f"NAS-Logging aktiv: {self.path}")
        line = (self.format(record) + "\n").encode("utf-8")
        try:
            self._write(line)
        except Exception:
            # Haeufigster Fall: NAS hat die Verbindung nach Leerlauf getrennt ->
            # einmal sofort neu verbinden, erst danach als offline werten.
            try:
                self._disconnect()
                self._open()
                self._write(line)
            except Exception as e:
                self._go_offline(e)

    def close(self):
        try:
            self._disconnect()
        except Exception:
            pass
        super().close()


class FtpsLogHandler(NasLogHandler):
    """NAS per FTPS (FTP mit TLS-Verschluesselung). Nur Python-Standardbibliothek."""

    def __init__(self, host, port, folder, filename, user, password, **kw):
        folder = "/" + folder.strip("/") if folder.strip("/") else ""
        super().__init__(f"{folder}/{filename}", **kw)
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.folder = folder
        self.ftp = None

    def _connect(self):
        ftp = ftplib.FTP_TLS(timeout=10)
        ftp.connect(self.host, self.port)
        ftp.login(self.user, self.password)
        ftp.prot_p()                 # auch die Datenverbindung verschluesseln
        ftp.voidcmd("TYPE I")
        parts = [p for p in self.folder.split("/") if p]
        for i in range(1, len(parts) + 1):  # Ordner anlegen, falls noetig
            try:
                ftp.mkd("/" + "/".join(parts[:i]))
            except ftplib.error_perm:
                pass  # existiert bereits
        self.ftp = ftp

    def _size(self, path):
        return self.ftp.size(path)

    def _move(self, src, dst):
        self.ftp.size(src)  # wirft Fehler, wenn src fehlt
        try:
            self.ftp.delete(dst)
        except ftplib.error_perm:
            pass
        self.ftp.rename(src, dst)

    def _delete(self, path):
        self.ftp.delete(path)

    def _append(self, path, data):
        self.ftp.storbinary(f"APPE {path}", io.BytesIO(data))

    def _disconnect(self):
        if self.ftp:
            ftp, self.ftp = self.ftp, None
            try:
                ftp.quit()
            except Exception:
                ftp.close()


class SmbLogHandler(NasLogHandler):
    """NAS per SMB (Windows-Freigabe). Benoetigt das pip-Paket smbprotocol."""

    def __init__(self, server, share, folder, filename, user, password, **kw):
        unc = "\\\\" + server + "\\" + share
        if folder.strip("\\/"):
            unc += "\\" + folder.strip("\\/").replace("/", "\\")
        super().__init__(unc + "\\" + filename, **kw)
        self.server, self.user, self.password, self.folder = server, user, password, unc

    def _connect(self):
        smbclient.register_session(self.server, username=self.user or None,
                                   password=self.password or None, connection_timeout=5)
        smbclient.makedirs(self.folder, exist_ok=True)

    def _size(self, path):
        return smbclient.stat(path).st_size

    def _move(self, src, dst):
        smbclient.replace(src, dst)

    def _delete(self, path):
        smbclient.remove(path)

    def _append(self, path, data):
        with smbclient.open_file(path, mode="ab") as f:
            f.write(data)

    def _disconnect(self):
        smbclient.reset_connection_cache()


_log_listener = None


def setup_logging(config):
    """Konsole + Logdatei auf dem NAS (FTPS oder SMB).
    Ist kein Zugriff moeglich, laeuft die Steuerung ohne Logdatei weiter."""
    global _log_listener
    level = getattr(logging, str(config.get("log_level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    if _log_listener:
        _log_listener.stop()
        _log_listener = None
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    logger.addHandler(console)
    # Flask/Werkzeug-Zugriffszeilen ("GET /status 200") nicht ins Log
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    nas = config.get("log_nas") or {}
    if not nas.get("server"):
        console_note("Kein NAS in config.json (log_nas) eingetragen - laufe ohne Logdatei")
        return None

    common = dict(
        folder=nas.get("ordner", ""), filename=nas.get("datei", "heizstab.log"),
        user=nas.get("benutzer", ""), password=nas.get("passwort", ""),
        max_bytes=int(float(config.get("log_max_mb", 1)) * 1024 * 1024),
        backups=int(config.get("log_anzahl_dateien", 3)),
        retry=float(nas.get("neuer_versuch_sekunden", 300)),
    )
    protocol = str(nas.get("protokoll", "ftps")).lower()
    if protocol == "smb":
        if smbclient is None:
            console_note(f"SMB-Modul nicht ladbar ({SMB_IMPORT_ERROR}) - laufe ohne Logdatei. "
                         f"Tipp: \"protokoll\": \"ftps\" braucht keine Zusatzpakete.")
            return None
        handler = SmbLogHandler(server=nas["server"], share=nas.get("freigabe", ""), **common)
    elif protocol == "ftps":
        handler = FtpsLogHandler(host=nas["server"], port=nas.get("port", 21), **common)
    else:
        console_note(f"Unbekanntes protokoll '{protocol}' in log_nas (ftps oder smb) - laufe ohne Logdatei")
        return None
    handler.setFormatter(fmt)
    handler.setLevel(level)

    # Begrenzte Warteschlange: haengt das NAS, gehen im Extremfall Logzeilen verloren,
    # aber nie Speicher oder Rechenzeit der Steuerung.
    q = queue.Queue(maxsize=1000)
    queue_handler = QueueHandler(q)
    queue_handler.setLevel(level)
    logger.addHandler(queue_handler)
    _log_listener = QueueListener(q, handler, respect_handler_level=True)
    _log_listener.start()
    log(f"Heizstab-Log ({protocol.upper()}): {nas['server']} {handler.path} "
        f"(max. {common['max_bytes'] // 1024} KB, {common['backups']} alte Dateien, "
        f"Level {logging.getLevelName(level)})")
    return handler


STATE_SAVE_INTERVAL = 60  # s; schont die SD-Karte bei haeufigen Kommawerten von evcc
_last_saved = {"time": 0.0, "enabled": None}


def save_evcc_state(force=False):
    """Freigabe-Aenderungen sofort speichern, Strom-Aenderungen hoechstens alle 60 s.
    Nach einem Neustart fehlt so schlimmstenfalls der Strom der letzten Minute -
    evcc schickt im Regelbetrieb ohnehin laufend neue Werte."""
    now = time.time()
    if not force and state["enabled"] == _last_saved["enabled"] \
            and now - _last_saved["time"] < STATE_SAVE_INTERVAL:
        return
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": state["enabled"], "ampere": state["ampere"]}, f)
        _last_saved.update(time=now, enabled=state["enabled"])
    except Exception as e:
        log(f"Zustand konnte nicht gespeichert werden: {e}", WARNUNG)


def load_evcc_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        state["enabled"] = bool(saved.get("enabled", False))
        state["ampere"] = float(saved.get("ampere", 0.0))
        log(f"Letzte evcc-Vorgaben geladen: enabled={state['enabled']}, {state['ampere']} A")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Gespeicherter Zustand unlesbar: {e}", WARNUNG)


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"config.json konnte nicht geladen werden ({CONFIG_FILE}): {e}", FEHLER)
        return {}


def relays_for_stage(config, stage):
    return {
        0: [],
        1: config.get("relais_stufe_1", [2]),
        2: config.get("relais_stufe_2", [1, 2]),
        3: config.get("relais_stufe_3", [1, 2, 3]),
    }.get(stage, [])


def managed_relays(config):
    """Alle Relais, die in irgendeiner Stufe vorkommen. Nur diese werden geschaltet/gelesen,
    andere Relais am ESP bleiben unberuehrt."""
    relays = set()
    for stage in (1, 2, 3):
        relays.update(relays_for_stage(config, stage))
    return sorted(relays)


def stage_power(config, stage):
    return stage * config.get("leistung_pro_stufe", 1000)


class EspAuthError(RuntimeError):
    pass


def tasmota_cmd(config, cmnd, attempts=None):
    """Sendet einen Befehl an den ESP (mit Login, falls konfiguriert).
    Wiederholt bei Zeitueberschreitung/Verbindungsfehler, da der ESP-01 per WLAN
    gelegentlich nicht sofort antwortet."""
    esp_ip = config.get("esp_heizstab_ip", "10.0.10.98")
    timeout = config.get("esp_timeout", 3)
    attempts = attempts or config.get("esp_versuche", 3)
    params = {"cmnd": cmnd}
    if config.get("esp_password"):
        params["user"] = config.get("esp_user", "admin")
        params["password"] = config["esp_password"]

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            res = requests.get(f"http://{esp_ip}/cm", params=params, timeout=timeout)
            if res.status_code == 401:
                raise EspAuthError("401 Nicht autorisiert - esp_user/esp_password in config.json pruefen")
            res.raise_for_status()
            if attempt > 1:
                log(f"ESP antwortet im Versuch {attempt}/{attempts} ({cmnd})", DEBUG)
            return res.json()
        except EspAuthError:
            raise
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(0.3)
    raise RuntimeError(f"{attempts} Versuche fehlgeschlagen ({cmnd}): {last_error}")


# ---------------------------------------------------------------- ESP / Tasmota

def send_relays(stage):
    """Schaltet alle drei Relais mit EINEM Request (Tasmota-Backlog).
    Gibt True zurueck, wenn der ESP den Befehl angenommen hat."""
    config = load_config()
    active = relays_for_stage(config, stage)
    parts = [f"Power{r} {'ON' if r in active else 'OFF'}" for r in managed_relays(config)]
    ok = True
    try:
        tasmota_cmd(config, "Backlog " + "; ".join(parts))
    except Exception as e:
        ok = False
        log(f"Schalten fehlgeschlagen: {e}", FEHLER)
    log(f"Relais fuer Stufe {stage} gesendet (aktiv: {active}) {'OK' if ok else 'FEHLGESCHLAGEN'}", INFO if ok else FEHLER)
    state["esp_cache"] = None  # naechste Statusabfrage soll frisch lesen
    return ok


def switch_stage(stage, reason):
    with lock:
        if stage != state["current_stage"]:
            log(f"STUFENWECHSEL {state['current_stage']} -> {stage} ({reason})")
            state["current_stage"] = stage
            state["last_switch"] = time.time()
        state["target_stage"] = stage
        state["mismatch_counter"] = 0
        send_relays(stage)


def read_esp_stage():
    """Liest den echten Relaiszustand. Rueckgabe: (actual_stage, online)."""
    config = load_config()
    try:
        data = tasmota_cmd(config, "State", attempts=2)
    except Exception as e:
        log(f"ESP nicht erreichbar: {e}", WARNUNG)
        return 0, False

    active = {r for r in managed_relays(config) if data.get(f"POWER{r}") == "ON"}
    for stage in (3, 2, 1):
        if active == set(relays_for_stage(config, stage)):
            return stage, True
    if not active:
        return 0, True
    log(f"Unbekannte Relaiskombination {sorted(active)}", WARNUNG)
    return len(active), True


def poll_esp():
    """ESP-Zustand (gecacht) lesen und Soll/Ist-Abgleich durchfuehren."""
    now = time.time()
    with lock:
        cache = state["esp_cache"]
        if cache and now - cache[0] < ESP_CACHE_SECONDS:
            return cache[1], cache[2]

    actual, online = read_esp_stage()

    with lock:
        state["esp_cache"] = (time.time(), actual, online)
        soll = state["current_stage"]
        if online and actual == soll:
            state["mismatch_counter"] = 0
        else:
            state["mismatch_counter"] += 1
            n = state["mismatch_counter"]
            if online:
                log(f"Abweichung {n}/3: Soll Stufe {soll}, Ist Stufe {actual}", WARNUNG)
                if n >= 3:
                    log("3 Abweichungen in Folge -> Relais werden neu gesendet", WARNUNG)
                    switch_stage(soll, "Soll/Ist-Abgleich")
            elif n >= 3 and soll != 0:
                log("ESP 3x nicht erreichbar -> interner Status auf Stufe 0", FEHLER)
                state["current_stage"] = 0
                state["target_stage"] = 0
    return actual, online


def get_temperature():
    now = time.time()
    cache = state["temp_cache"]
    if cache and now - cache[0] < TEMP_CACHE_SECONDS:
        return cache[1]

    config = load_config()
    temp = None
    timeout_val = config.get("ha_timeout", 5)
    try:
        if config.get("use_home_assistant", True):
            headers = {"Authorization": f"Bearer {config.get('ha_token')}"}
            url = f"{config.get('ha_url')}/api/states/{config.get('ha_sensor_entity')}"
            res = requests.get(url, headers=headers, timeout=timeout_val)
            res.raise_for_status()
            temp = float(res.json().get("state"))
        else:
            url = f"http://{config.get('tasmota_thr316d_ip')}/cm?cmnd=Status%2010"
            sns = requests.get(url, timeout=timeout_val).json().get("StatusSNS", {})
            for sensor in ("DS18B20", "AM2301"):
                if sensor in sns:
                    temp = float(sns[sensor].get("Temperature"))
                    break
    except Exception as e:
        log(f"Temperatur nicht lesbar: {e}", WARNUNG)

    state["temp_cache"] = (now, temp)
    return temp


# ---------------------------------------------------------------- Regelung

def amps_to_watts(amps, config):
    """So rechnet auch evcc: Strom pro Phase x Spannung x Phasen."""
    return amps * config.get("spannung", 230) * config.get("phasen", 3)


def thresholds(config):
    """Einschalt- und Abschaltschwellen in W je Stufe. Einzige Stelle fuer diese Rechnung -
    Steuerung und Weboberflaeche benutzen beide diese Funktion."""
    w = config.get("leistung_pro_stufe", 1000)
    p = config.get("puffer_einschalten", 200)
    ein = {n: n * w + p for n in (1, 2, 3)}
    aus = {n: config.get(f"abschalt_unter_s{n}", n * w) for n in (1, 2, 3)}
    return ein, aus


def desired_stage_for(watts, current, config):
    """Stufe mit Hysterese: hoch nur ueber Einschalt-, runter nur unter Abschaltschwelle."""
    ein, aus = thresholds(config)
    stage = current
    while stage < 3 and watts >= ein[stage + 1]:
        stage += 1
    while stage > 0 and watts < aus[stage]:
        stage -= 1
    return stage


def evaluate():
    """Prueft Freigabe, Watchdog, Verzoegerungen und schaltet ggf. um.
    Wird bei jedem evcc-Aufruf ausgefuehrt, damit Timer auch ohne neue Sollwerte ablaufen."""
    config = load_config()
    now = time.time()
    with lock:
        current = state["current_stage"]

        # Watchdog: kein Kontakt zu evcc -> aus
        timeout = config.get("evcc_timeout_seconds", 300)
        if current > 0 and now - state["last_evcc_contact"] > timeout:
            switch_stage(0, f"Watchdog: {timeout}s kein Kontakt zu evcc")
            return

        # Nicht freigegeben -> sofort aus (ohne Verzoegerung/Mindestlaufzeit)
        if not state["enabled"]:
            if current > 0:
                switch_stage(0, "evcc hat deaktiviert")
            return

        if state["ampere"] <= 0 and not state.get("warned_no_current"):
            log("Freigegeben, aber evcc hat noch keinen Strom gesendet (/setcurrent fehlt)", WARNUNG)
            state["warned_no_current"] = True

        watts = amps_to_watts(state["ampere"], config)
        desired = desired_stage_for(watts, current, config)

        if desired == current:
            state["target_stage"] = current
            return

        if desired != state["target_stage"]:
            state["target_stage"] = desired
            state["target_since"] = now
            log(f"Neue Zielstufe {desired} (aktuell {current}, {watts:.0f} W vorgegeben)")

        delay = config.get("zeit_bis_zuschalten", 60) if desired > current else config.get("zeit_bis_abschalten", 10)
        waited = now - state["target_since"]
        min_run = config.get("mindest_laufzeit", 300)
        since_switch = now - state["last_switch"]

        if waited < delay:
            log(f"Warte auf Verzoegerung: {waited:.0f}/{delay}s bis Stufe {desired}", DEBUG)
            return
        if current > 0 and since_switch < min_run:
            log(f"Mindestlaufzeit aktiv: noch {min_run - since_switch:.0f}s", DEBUG)
            return

        switch_stage(desired, f"{watts:.0f} W vorgegeben")


def explain(config, now, current, desired, ein):
    """Warum wird (nicht) geschaltet? Gleiche Regeln wie evaluate(), aber ohne zu schalten."""
    if now - state["last_evcc_contact"] > config.get("evcc_timeout_seconds", 300) and current > 0:
        return "Watchdog: zu lange kein Kontakt zu evcc - Heizstab wird abgeschaltet"
    if not state["enabled"]:
        return "evcc hat nicht freigegeben - Heizstab aus"
    if desired == current:
        if current == 0:
            return f"Sollwert liegt unter der Einschaltschwelle von Stufe 1 ({ein[1]:.0f} W) - aus"
        return f"Stufe {current} passt zum Sollwert"
    if state["target_stage"] != desired:
        return f"Wechsel auf Stufe {desired} wird bei der naechsten evcc-Abfrage eingeleitet"
    delay = config.get("zeit_bis_zuschalten", 60) if desired > current else config.get("zeit_bis_abschalten", 10)
    rest = delay - (now - state["target_since"])
    if rest > 0:
        return f"Wechsel auf Stufe {desired} in {rest:.0f} s (Verzoegerung {delay} s)"
    min_rest = config.get("mindest_laufzeit", 300) - (now - state["last_switch"])
    if current > 0 and min_rest > 0:
        return f"Wechsel auf Stufe {desired} in {min_rest:.0f} s (Mindestlaufzeit)"
    return f"Wechsel auf Stufe {desired} bei der naechsten evcc-Abfrage"


def calculation(amps=None):
    """Leistungsberechnung fuer die Weboberflaeche.
    amps=None: aktueller Sollwert von evcc; sonst 'Was waere wenn'-Rechnung."""
    config = load_config()
    now = time.time()
    with lock:
        current = state["current_stage"]
        what_if = amps is not None
        a = amps if what_if else state["ampere"]
        watts = amps_to_watts(a, config)
        desired = desired_stage_for(watts, current, config)
        ein, aus = thresholds(config)
        reason = None if what_if else explain(config, now, current, desired, ein)
        enabled = state["enabled"]
    factor = amps_to_watts(1, config)
    return {
        "was_waere_wenn": what_if,
        "ampere": a,
        "watt": watts,
        "spannung": config.get("spannung", 230),
        "phasen": config.get("phasen", 3),
        "freigabe": enabled,
        "aktuelle_stufe": current,
        "leistung_aktuell": stage_power(config, current),
        "ziel_stufe": desired,
        "leistung_ziel": stage_power(config, desired),
        "grund": reason,
        # evcc sendet ganze Ampere: kleinster Wert, bei dem Stufe 1 einschaltet
        "min_ampere_stufe1": math.ceil(ein[1] / factor - 1e-9),
        # mit maxcurrentmillis (Kommawerte) in der evcc.yaml genuegt dieser Wert
        "min_ampere_stufe1_genau": math.ceil(ein[1] / factor * 100 - 1e-9) / 100,
        "stufen": [
            {"stufe": n, "relais": relays_for_stage(config, n), "leistung": stage_power(config, n),
             "ein_w": ein[n], "ein_a": ein[n] / factor, "aus_w": aus[n], "aus_a": aus[n] / factor}
            for n in (1, 2, 3)
        ],
    }


def parse_float(raw):
    try:
        if raw is None or "<" in str(raw) or ">" in str(raw):
            return 0.0
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------- HTTP-Endpunkte

EVCC_PATHS = ("/status", "/enable", "/setcurrent")


@app.before_request
def on_request():
    # Nur Anfragen von evcc zaehlen fuer den Watchdog - sonst wuerde eine offene
    # Weboberflaeche einen evcc-Ausfall verdecken.
    if request.path in EVCC_PATHS:
        state["last_evcc_contact"] = time.time()
        log(f"REQ {request.path} {request.args.to_dict()} von {request.remote_addr}", DEBUG)


@app.route("/enable", methods=["GET"])
def enable():
    val = str(request.args.get("enable", "")).strip().lower()
    with lock:
        state["enabled"] = val in ("true", "1", "on")
        save_evcc_state()
        log(f"Freigabe von evcc: enabled={state['enabled']}")
    evaluate()
    return jsonify({"status": "ok", "enabled": state["enabled"]})


@app.route("/setcurrent", methods=["GET"])
def set_current():
    amps = parse_float(request.args.get("ampere"))
    with lock:
        state["ampere"] = amps
        state["warned_no_current"] = False
        save_evcc_state()
    # DEBUG: mit Kommawerten kommt das ggf. alle 10 s. Stufenwechsel stehen weiterhin
    # als INFO im Log ("Neue Zielstufe ...", "STUFENWECHSEL ..." inkl. Watt).
    log(f"Sollstrom von evcc: {amps:.2f} A = {amps_to_watts(amps, load_config()):.0f} W", DEBUG)
    evaluate()
    return jsonify({"status": "ok", "stage": state["current_stage"], "target": state["target_stage"]})


@app.route("/status", methods=["GET"])
def status():
    evaluate()
    config = load_config()
    actual, online = poll_esp()
    power = stage_power(config, actual) if online else 0
    res = {
        "status": "C" if power > 0 else "B",
        "enabled": state["enabled"],
        "power": power,
        "stage": actual,
        "esp_online": online,
        "temp": get_temperature(),
    }
    return jsonify(res)


# ---------------------------------------------------------------- Weboberflaeche

LOG_KEYS = ("log_level", "log_max_mb", "log_anzahl_dateien")


def webui_status():
    since = time.time() - state["last_evcc_contact"]
    return {
        "Freigabe evcc": "ja" if state["enabled"] else "nein",
        "Sollstrom": f"{state['ampere']:g} A",
        "Stufe": state["current_stage"],
        "Letzter evcc-Kontakt": f"vor {since:.0f} s",
    }


def webui_saved(old, new, changed):
    # Die meisten Einstellungen liest das Skript bei jeder Anfrage neu.
    # Nur das Logging muss neu aufgesetzt werden.
    if any(k in LOG_KEYS or k.startswith("log_nas.") for k in changed):
        setup_logging(new)


import webui  # noqa: E402

webui.register(app, CONFIG_FILE, load_config, on_saved=webui_saved,
               get_status=webui_status, get_calculation=calculation, log=log)


if __name__ == "__main__":
    setup_logging(load_config())
    log(f"Heizstab-Steuerung gestartet, Config: {CONFIG_FILE}")
    load_evcc_state()
    app.run(host="0.0.0.0", port=5000, threaded=True)
