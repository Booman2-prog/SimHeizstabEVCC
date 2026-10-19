"""Tests fuer die Leistungsberechnung (control.calculation) und deren Anzeige-Endpunkt."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "aktuell"))
import control  # noqa: E402

CFG = {"leistung_pro_stufe": 1000, "puffer_einschalten": 200,
       "abschalt_unter_s1": 1000, "abschalt_unter_s2": 2000, "abschalt_unter_s3": 3000,
       "zeit_bis_zuschalten": 0, "zeit_bis_abschalten": 0, "mindest_laufzeit": 0,
       "relais_stufe_1": [4], "relais_stufe_2": [3, 4], "relais_stufe_3": [2, 3, 4],
       "web_passwort": ""}


@pytest.fixture
def cfg(monkeypatch):
    c = dict(CFG)
    monkeypatch.setattr(control, "load_config", lambda: c)
    control.state.update({"enabled": True, "ampere": 0.0, "current_stage": 0, "target_stage": 0,
                          "target_since": 0.0, "last_switch": 0.0, "last_evcc_contact": time.time()})
    return c


def test_beispiel_2_ampere(cfg):
    control.state["ampere"] = 2
    d = control.calculation()
    assert d["watt"] == 1380 and d["ziel_stufe"] == 1 and d["leistung_ziel"] == 1000
    assert [(s["ein_w"], s["aus_w"]) for s in d["stufen"]] == [(1200, 1000), (2200, 2000), (3200, 3000)]
    assert round(d["stufen"][0]["ein_a"], 2) == 1.74
    assert d["min_ampere_stufe1"] == 2 and d["min_ampere_stufe1_genau"] == 1.74
    assert d["stufen"][2]["relais"] == [2, 3, 4]


def test_was_waere_wenn_aendert_nichts(cfg):
    d = control.calculation(5)
    assert d["was_waere_wenn"] and d["watt"] == 3450 and d["ziel_stufe"] == 3
    assert control.state["ampere"] == 0.0 and control.state["current_stage"] == 0


def test_hysterese_von_aktueller_stufe(cfg):
    control.state["current_stage"] = 3
    # 2,2 A = 1518 W: unter Abschaltschwelle 3 und 2, ueber der von Stufe 1
    assert control.calculation(2.2)["ziel_stufe"] == 1
    control.state["current_stage"] = 0
    # 3 A = 2070 W: von 0 aus nur bis Stufe 1 (Stufe 2 erst ab 2200 W)
    assert control.calculation(3)["ziel_stufe"] == 1


def test_anzeige_und_steuerung_nutzen_gleiche_schwellen(cfg):
    cfg["leistung_pro_stufe"] = 1500
    ein, _ = control.thresholds(cfg)
    assert ein[1] == 1700
    assert control.calculation()["stufen"][0]["ein_w"] == 1700
    assert control.desired_stage_for(1699, 0, cfg) == 0 and control.desired_stage_for(1700, 0, cfg) == 1


def test_einphasig(cfg):
    cfg["phasen"] = 1
    d = control.calculation(6)
    assert d["watt"] == 1380 and d["min_ampere_stufe1"] == 6


@pytest.mark.parametrize("setup,text", [
    (dict(enabled=False), "nicht freigegeben"),
    (dict(ampere=0), "unter der Einschaltschwelle"),
    (dict(ampere=2, current_stage=1, target_stage=1), "Stufe 1 passt"),
])
def test_erklaerung(cfg, setup, text):
    control.state.update(setup)
    assert text in control.calculation()["grund"]


def test_erklaerung_verzoegerung(cfg, monkeypatch):
    cfg["zeit_bis_zuschalten"] = 60
    monkeypatch.setattr(control, "switch_stage", lambda *a: None)
    control.state["ampere"] = 5
    control.evaluate()  # leitet den Wechsel ein, Timer laeuft
    assert "in 60 s (Verzoegerung 60 s)" in control.calculation()["grund"]


def test_endpunkt(cfg):
    client = control.app.test_client()
    control.state["ampere"] = 2
    assert client.get("/config/rechnung").get_json()["watt"] == 1380
    assert client.get("/config/rechnung?ampere=2,5").get_json()["watt"] == 1725
    assert client.get("/config/rechnung?ampere=abc").status_code == 400
    assert client.get("/config/rechnung?ampere=500").status_code == 400
    html = client.get("/config").get_data(as_text=True)
    assert "Leistungsberechnung" in html and "/config/rechnung" in html


def test_kommawerte_regeln_genauer(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(control, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(control, "switch_stage", lambda *a: None)
    client = control.app.test_client()
    # 1300 W Ueberschuss: ganze Ampere -> 1 A = 690 W -> aus; mit Kommawert 1,88 A -> Stufe 1
    assert control.calculation(1)["ziel_stufe"] == 0
    client.get("/setcurrent?ampere=1.884")
    assert control.state["ampere"] == 1.884
    assert control.calculation()["ziel_stufe"] == 1
    # 2500 W: ganze Ampere -> 3 A = 2070 W -> nur Stufe 1; mit 3,62 A -> Stufe 2
    assert control.calculation(3)["ziel_stufe"] == 1
    assert control.calculation(3.62)["ziel_stufe"] == 2


def test_strom_wird_gedrosselt_gespeichert(cfg, monkeypatch, tmp_path):
    path = tmp_path / "s.json"
    monkeypatch.setattr(control, "STATE_FILE", str(path))
    monkeypatch.setattr(control, "switch_stage", lambda *a: None)
    control._last_saved.update(time=0.0, enabled=None)
    writes = []
    real_open = open
    def counting_open(f, mode="r", *a, **k):
        if f == str(path) and "w" in mode:
            writes.append(1)
        return real_open(f, mode, *a, **k)
    monkeypatch.setattr("builtins.open", counting_open)
    client = control.app.test_client()
    for i in range(20):                      # evcc schickt 20 neue Kommawerte
        client.get(f"/setcurrent?ampere={1.8 + i / 100}")
    assert len(writes) == 1                  # nur einmal auf die SD-Karte
    client.get("/enable?enable=false")       # Freigabe-Aenderung -> sofort speichern
    assert len(writes) == 2
