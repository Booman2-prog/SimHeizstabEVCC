"""Tests fuer control.py mit simuliertem Tasmota-ESP (kein echtes Netzwerk noetig)."""
import json
import re
import time

import pytest

import control


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeTasmota:
    def __init__(self):
        self.relays = {1: "OFF", 2: "OFF", 3: "OFF", 4: "OFF"}
        self.online = True
        self.password = None  # gesetzt -> ESP verlangt Login
        self.fail_next = 0    # so viele Requests absichtlich scheitern lassen
        self.calls = []

    def get(self, url, params=None, **kwargs):
        if "api/states" in url:
            return FakeResponse({"state": "55.0"})
        if not self.online:
            raise ConnectionError("ESP offline")
        params = params or {}
        if self.password and params.get("password") != self.password:
            resp = FakeResponse({})
            resp.status_code = 401
            return resp
        cmnd = params.get("cmnd", "")
        self.calls.append(cmnd)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TimeoutError("ESP antwortet nicht")
        if cmnd.startswith("Backlog "):
            for part in cmnd[len("Backlog "):].split(";"):
                m = re.fullmatch(r"Power(\d) (ON|OFF)", part.strip())
                assert m, f"unerwarteter Backlog-Teil {part!r}"
                self.relays[int(m.group(1))] = m.group(2)
            return FakeResponse({})
        if cmnd == "State":
            return FakeResponse({f"POWER{k}": v for k, v in self.relays.items()})
        raise AssertionError(f"unerwarteter Aufruf {url} {params}")


CONFIG = {
    "esp_heizstab_ip": "esp",
    "use_home_assistant": True,
    "ha_url": "http://ha",
    "relais_stufe_1": [2],
    "relais_stufe_2": [1, 2],
    "relais_stufe_3": [1, 2, 3],
    "evcc_timeout_seconds": 300,
    "puffer_einschalten": 200,
    "abschalt_unter_s3": 3000,
    "abschalt_unter_s2": 2000,
    "abschalt_unter_s1": 1000,
    "zeit_bis_zuschalten": 0,
    "zeit_bis_abschalten": 0,
    "mindest_laufzeit": 0,
}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    esp = FakeTasmota()
    cfg = dict(CONFIG)
    monkeypatch.setattr(control, "STATE_FILE", str(tmp_path / "evcc_state.json"))
    monkeypatch.setattr(control.requests, "get", esp.get)
    monkeypatch.setattr(control, "load_config", lambda: cfg)
    monkeypatch.setattr(control, "ESP_CACHE_SECONDS", 0)
    control.state.update({
        "enabled": False, "ampere": 0.0, "current_stage": 0, "target_stage": 0,
        "target_since": 0.0, "last_switch": 0.0, "last_evcc_contact": time.time(),
        "mismatch_counter": 0, "esp_cache": None, "temp_cache": None,
    })
    return control.app.test_client(), esp, cfg


def test_schnell_dann_aus(setup):
    client, esp, _ = setup
    client.get("/setcurrent?ampere=5")
    assert client.get("/status").get_json()["power"] == 0  # noch nicht freigegeben

    client.get("/enable?enable=true")
    st = client.get("/status").get_json()
    assert st["power"] == 3000 and st["status"] == "C" and st["enabled"] is True
    assert esp.relays == {1: "ON", 2: "ON", 3: "ON", 4: "OFF"}

    client.get("/enable?enable=false")
    st = client.get("/status").get_json()
    assert st["power"] == 0 and st["status"] == "B"
    assert esp.relays == {1: "OFF", 2: "OFF", 3: "OFF", 4: "OFF"}


def test_stufen_nach_strom(setup):
    client, esp, _ = setup
    client.get("/enable?enable=true")
    for amps, stage in [(2, 1), (4, 2), (5, 3), (1, 0)]:
        client.get(f"/setcurrent?ampere={amps}")
        assert client.get("/status").get_json()["stage"] == stage, amps


def test_hysterese_kein_sprung_von_3_auf_1(setup):
    client, _, _ = setup
    client.get("/enable?enable=true")
    client.get("/setcurrent?ampere=5")          # 3450 W -> Stufe 3
    client.get("/setcurrent?ampere=3.1")        # 2139 W: unter 3000, ueber 2000 -> Stufe 2
    assert client.get("/status").get_json()["stage"] == 2


def test_verzoegerung_laeuft_ueber_status_ab(setup):
    client, _, cfg = setup
    cfg["zeit_bis_zuschalten"] = 1
    client.get("/enable?enable=true")
    client.get("/setcurrent?ampere=5")
    assert client.get("/status").get_json()["stage"] == 0  # wartet noch
    time.sleep(1.1)
    # kein neuer setcurrent noetig - die Statusabfrage schaltet weiter
    assert client.get("/status").get_json()["stage"] == 3


def test_ampere_platzhalter(setup):
    client, _, _ = setup
    assert client.get("/setcurrent?ampere=%3Cno%20value%3E").status_code == 200
    assert control.state["ampere"] == 0.0


def test_esp_mit_passwort(setup):
    client, esp, cfg = setup
    esp.password = "geheim"
    client.get("/enable?enable=true")
    client.get("/setcurrent?ampere=5")
    st = client.get("/status").get_json()
    assert st["power"] == 0 and st["esp_online"] is False  # ohne Login: 401

    cfg["esp_user"] = "admin"
    cfg["esp_password"] = "geheim"
    client.get("/setcurrent?ampere=5.1")  # erneut auswerten
    for _ in range(4):
        st = client.get("/status").get_json()
    assert st["power"] == 3000 and esp.relays == {1: "ON", 2: "ON", 3: "ON", 4: "OFF"}


def test_vorgaben_ueberleben_neustart(setup):
    client, esp, _ = setup
    client.get("/setcurrent?ampere=5")
    client.get("/enable?enable=true")
    # Neustart simulieren: Speicher leeren, dann gespeicherten Zustand laden
    control.state.update({"enabled": False, "ampere": 0.0, "current_stage": 0})
    control.load_evcc_state()
    assert control.state["enabled"] is True and control.state["ampere"] == 5.0
    # evcc sendet nach dem Neustart nur /enable, aber kein /setcurrent
    client.get("/enable?enable=true")
    assert client.get("/status").get_json()["power"] == 3000


def test_wackliges_wlan_wird_wiederholt(setup, monkeypatch):
    client, esp, _ = setup
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    client.get("/setcurrent?ampere=5")
    esp.fail_next = 2  # die ersten zwei Versuche scheitern
    client.get("/enable?enable=true")
    assert esp.relays == {1: "ON", 2: "ON", 3: "ON", 4: "OFF"}
    assert client.get("/status").get_json()["power"] == 3000


def test_alle_relais_in_einem_request(setup):
    client, esp, _ = setup
    client.get("/setcurrent?ampere=4")
    client.get("/enable?enable=true")
    switch_calls = [c for c in esp.calls if c.startswith("Backlog")]
    assert switch_calls == ["Backlog Power1 ON; Power2 ON; Power3 OFF"]


def test_relais_2_bis_4_relais_1_bleibt_unberuehrt(setup):
    client, esp, cfg = setup
    cfg.update({"relais_stufe_1": [4], "relais_stufe_2": [3, 4], "relais_stufe_3": [2, 3, 4]})
    esp.relays[1] = "ON"  # Relais 1 gehoert nicht zum Heizstab
    client.get("/enable?enable=true")
    for amps, stage, expected in [
        (2, 1, {2: "OFF", 3: "OFF", 4: "ON"}),
        (4, 2, {2: "OFF", 3: "ON", 4: "ON"}),
        (5, 3, {2: "ON", 3: "ON", 4: "ON"}),
    ]:
        client.get(f"/setcurrent?ampere={amps}")
        st = client.get("/status").get_json()
        assert st["stage"] == stage and st["power"] == stage * 1000, amps
        assert {r: esp.relays[r] for r in (2, 3, 4)} == expected, amps
    client.get("/enable?enable=false")
    assert esp.relays == {1: "ON", 2: "OFF", 3: "OFF", 4: "OFF"}


def test_esp_offline_meldet_0w(setup):
    client, esp, _ = setup
    client.get("/enable?enable=true")
    client.get("/setcurrent?ampere=5")
    esp.online = False
    st = client.get("/status").get_json()
    assert st["power"] == 0 and st["esp_online"] is False

