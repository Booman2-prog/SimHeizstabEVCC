"""Tests fuer die Weboberflaeche (Install/webui.py) - arbeitet nur mit einer Kopie der Config."""
import base64
import json
import os
import re
import sys
import time

import pytest
from flask import Flask

import webui  # noqa: E402

BASE = {
    "esp_heizstab_ip": "10.0.10.98", "esp_user": "admin", "esp_password": "espgeheim",
    "use_home_assistant": True, "ha_url": "http://ha:8123", "ha_token": "TOKEN123",
    "ha_sensor_entity": "sensor.x", "tasmota_thr316d_ip": "10.0.0.90",
    "relais_stufe_1": [4], "relais_stufe_2": [3, 4], "relais_stufe_3": [2, 3, 4],
    "ha_timeout": 5, "evcc_timeout_seconds": 300, "puffer_einschalten": 200,
    "abschalt_unter_s3": 3000, "abschalt_unter_s2": 2000, "abschalt_unter_s1": 1000,
    "zeit_bis_zuschalten": 0, "zeit_bis_abschalten": 0, "mindest_laufzeit": 0,
    "log_level": "INFO", "log_max_mb": 1, "log_anzahl_dateien": 3,
    "log_nas": {"protokoll": "ftps", "server": "10.10.10.2", "port": 21,
                "ordner": "/NAS-Nacht/EVCC-Logging", "datei": "heizstab.log",
                "benutzer": "Server", "passwort": "NASGEHEIM", "neuer_versuch_sekunden": 300},
    "web_benutzer": "admin", "web_passwort": "",
    "unbekannter_eintrag": {"bleibt": True},
}


@pytest.fixture
def ui(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(BASE), encoding="utf-8")
    saved_calls = []
    app = Flask(__name__)
    webui.register(app, str(path), lambda: json.loads(path.read_text(encoding="utf-8")),
                   on_saved=lambda o, n, c: saved_calls.append(c),
                   get_status=lambda: {"Stufe": 3}, log=lambda m: None)
    return app.test_client(), path, saved_calls


def form_from_page(html):
    """Alle Formularfelder so, wie der Browser sie absenden wuerde."""
    data = {"_csrf": re.search(r'name="_csrf" value="([^"]+)"', html).group(1)}
    for f in webui.FIELDS:
        k = f["key"]
        if f["type"] == "bool":
            if re.search(rf'name="{re.escape(k)}" checked', html):
                data[k] = "on"
        elif f["type"] == "select":
            sel = re.search(rf'name="{re.escape(k)}">(.*?)</select>', html, re.S).group(1)
            data[k] = re.search(r"<option selected>([^<]*)<", sel).group(1)
        elif f["type"] == "secret":
            data[k] = ""
        else:
            data[k] = re.search(rf'name="{re.escape(k)}" value="([^"]*)"', html).group(1)
    return data


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_seite_zeigt_werte_aber_keine_passwoerter(ui):
    client, _, _ = ui
    html = client.get("/config").get_data(as_text=True)
    assert "10.0.10.98" in html and "2, 3, 4" in html and "Stufe" in html
    for secret in ("espgeheim", "TOKEN123", "NASGEHEIM"):
        assert secret not in html
    assert "Kein Passwort" in html  # Warnhinweis


def test_aendern_und_speichern(ui):
    client, path, calls = ui
    data = form_from_page(client.get("/config").get_data(as_text=True))
    data.update({"zeit_bis_zuschalten": "60", "log_max_mb": "0,5", "relais_stufe_1": "4",
                 "relais_stufe_2": "4; 3"})
    data.pop("use_home_assistant", None)  # Checkbox abwaehlen
    r = client.post("/config", data=data)
    assert r.status_code == 303
    cfg = load(path)
    assert cfg["zeit_bis_zuschalten"] == 60 and cfg["log_max_mb"] == 0.5
    assert cfg["relais_stufe_2"] == [3, 4] and cfg["use_home_assistant"] is False
    # Passwoerter unveraendert, unbekannte Eintraege erhalten, Backup angelegt
    assert cfg["esp_password"] == "espgeheim" and cfg["log_nas"]["passwort"] == "NASGEHEIM"
    assert cfg["unbekannter_eintrag"] == {"bleibt": True}
    assert load(path.with_name("config.json.bak"))["zeit_bis_zuschalten"] == 0
    # "4; 3" ergibt sortiert [3, 4] = vorheriger Wert -> zaehlt nicht als Aenderung;
    # nicht vorhandene Eintraege mit Standardwert werden nicht neu hineingeschrieben
    assert set(calls[0]) == {"zeit_bis_zuschalten", "log_max_mb", "use_home_assistant"}
    assert "esp_timeout" not in cfg and "leistung_pro_stufe" not in cfg


def test_passwort_aendern(ui):
    client, path, _ = ui
    data = form_from_page(client.get("/config").get_data(as_text=True))
    data["log_nas.passwort"] = "NEU"
    client.post("/config", data=data)
    assert load(path)["log_nas"]["passwort"] == "NEU"


@pytest.mark.parametrize("key,value,msg", [
    ("relais_stufe_1", "a", "Relais"),
    ("relais_stufe_3", "9", "1 bis 8"),
    ("esp_versuche", "0", "erlaubt"),
    ("mindest_laufzeit", "abc", "Zahl"),
    ("abschalt_unter_s1", "1300", "Einschaltschwelle"),   # 1300 >= 1000+200
    ("esp_heizstab_ip", " ", "leer"),
])
def test_ungueltig_wird_nicht_gespeichert(ui, key, value, msg):
    client, path, _ = ui
    before = path.read_text(encoding="utf-8")
    data = form_from_page(client.get("/config").get_data(as_text=True))
    data[key] = value
    r = client.post("/config", data=data)
    assert r.status_code == 400 and msg in r.get_data(as_text=True)
    assert path.read_text(encoding="utf-8") == before


def test_csrf_pflicht(ui):
    client, path, _ = ui
    data = form_from_page(client.get("/config").get_data(as_text=True))
    data["_csrf"] = "falsch"
    assert client.post("/config", data=data).status_code == 400


def test_passwortschutz(ui):
    client, path, _ = ui
    cfg = load(path)
    cfg["web_passwort"] = "pw"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    assert client.get("/config").status_code == 401
    bad = base64.b64encode(b"admin:falsch").decode()
    assert client.get("/config", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    ok = base64.b64encode(b"admin:pw").decode()
    assert client.get("/config", headers={"Authorization": f"Basic {ok}"}).status_code == 200


def test_integration_control_watchdog_unberuehrt(monkeypatch):
    import control
    control.state["last_evcc_contact"] = 123.0
    client = control.app.test_client()
    html = client.get("/config").get_data(as_text=True)
    assert "Heizstab-Steuerung" in html
    assert control.state["last_evcc_contact"] == 123.0  # Webseite zaehlt nicht als evcc
    cfg = control.load_config()
    # esp_password ist "admin" wie der (sichtbare) Benutzername - daher hier nicht pruefbar
    for secret in (cfg.get("ha_token"), cfg.get("log_nas", {}).get("passwort")):
        if secret:
            assert secret not in html
