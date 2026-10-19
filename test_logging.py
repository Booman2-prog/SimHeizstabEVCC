"""Tests fuer das NAS-Logging in aktuell/control.py (mit simuliertem SMB-Server)."""
import io
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "aktuell"))
import control  # noqa: E402


class FakeSmb:
    """Minimaler Ersatz fuer das Modul smbclient (Dateien im Speicher)."""

    def __init__(self):
        self.files = {}
        self.online = True
        self.slow = 0.0
        self.sessions = []

    def _check(self):
        if not self.online:
            raise OSError("NAS nicht erreichbar")

    def register_session(self, server, username=None, password=None, connection_timeout=60):
        self._check()
        self.sessions.append((server, username, password))

    def makedirs(self, path, exist_ok=False):
        self._check()

    def stat(self, path):
        self._check()
        if path not in self.files:
            raise OSError("not found")
        return type("St", (), {"st_size": len(self.files[path])})()

    def replace(self, src, dst):
        self._check()
        if src not in self.files:
            raise OSError("not found")
        self.files[dst] = self.files.pop(src)

    def remove(self, path):
        self._check()
        self.files.pop(path, None)

    def reset_connection_cache(self):
        pass

    def open_file(self, path, mode="r"):
        self._check()
        time.sleep(self.slow)
        fake = self

        class F(io.BytesIO):
            def __exit__(self, *a):
                fake.files[path] = fake.files.get(path, b"") + self.getvalue()
                return super().__exit__(*a)
        return F()

    def text(self, name):
        return self.files.get(f"\\\\NAS-SERVER\\NAS-Nacht\\EVCC-Logging\\{name}", b"").decode("utf-8")


CFG = {"log_nas": {"protokoll": "smb", "server": "NAS-SERVER", "freigabe": "NAS-Nacht", "ordner": "EVCC-Logging",
                   "datei": "heizstab.log", "benutzer": "u", "passwort": "p",
                   "neuer_versuch_sekunden": 0.2}}


@pytest.fixture
def smb(monkeypatch):
    fake = FakeSmb()
    monkeypatch.setattr(control, "smbclient", fake)
    yield fake
    if control._log_listener:
        control._log_listener.stop()
        control._log_listener = None
    for h in list(control.logger.handlers):
        control.logger.removeHandler(h)


def flush():
    """Warten, bis der Hintergrund-Thread alle Zeilen geschrieben hat."""
    if control._log_listener:
        control._log_listener.stop()
        control._log_listener.start()


def test_schreibt_auf_nas_mit_zugangsdaten(smb):
    control.setup_logging(CFG)
    control.log("Stufe 3 geschaltet – Verzögerung 0 s")
    flush()
    assert "Stufe 3 geschaltet – Verzögerung 0 s" in smb.text("heizstab.log")
    assert smb.sessions[0] == ("NAS-SERVER", "u", "p")


def test_rotation_begrenzt_groesse(smb):
    control.setup_logging({**CFG, "log_max_mb": 0.01, "log_anzahl_dateien": 2})
    for i in range(1000):
        control.log(f"Zeile {i} " + "x" * 50)
    flush()
    names = sorted(k.rsplit("\\", 1)[1] for k in smb.files)
    assert names == ["heizstab.log", "heizstab.log.1", "heizstab.log.2"]
    assert all(len(v) <= int(0.01 * 1024 * 1024) for v in smb.files.values())
    assert "Zeile 999 " in smb.text("heizstab.log")


def test_nas_offline_beim_start_kein_absturz_und_spaeter_wieder(smb):
    smb.online = False
    control.setup_logging(CFG)
    control.log("geht verloren")
    flush()
    assert smb.files == {}
    smb.online = True
    time.sleep(0.3)  # neuer_versuch_sekunden abwarten
    control.log("wieder da")
    flush()
    assert "wieder da" in smb.text("heizstab.log")
    assert "geht verloren" not in smb.text("heizstab.log")


def test_nas_faellt_im_betrieb_aus(smb):
    control.setup_logging(CFG)
    control.log("vorher")
    flush()
    smb.online = False
    control.log("waehrend Ausfall")  # darf nicht abstuerzen
    flush()
    assert "vorher" in smb.text("heizstab.log")


def test_langsames_nas_blockiert_steuerung_nicht(smb):
    smb.slow = 1.0
    control.setup_logging(CFG)
    t = time.time()
    for _ in range(5):
        control.log("Zeile")
    assert time.time() - t < 0.2  # log() kehrt sofort zurueck


def test_ohne_smbprotocol_oder_ohne_nas_eintrag(smb, monkeypatch):
    assert control.setup_logging({}) is None
    monkeypatch.setattr(control, "smbclient", None)
    assert control.setup_logging(CFG) is None
    control.log("laeuft trotzdem")


def test_keine_lokale_datei(smb, tmp_path, monkeypatch):
    monkeypatch.setattr(control, "BASE_DIR", str(tmp_path))
    smb.online = False
    control.setup_logging(CFG)
    control.log("x")
    flush()
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ FTPS

import ftplib  # noqa: E402


class FakeFtpServer:
    def __init__(self):
        self.files, self.dirs = {}, {"/"}
        self.online = True
        self.drop_next_write = 0   # simuliert Trennung nach Leerlauf
        self.logins = []

    def client(self, timeout=None):
        srv = self

        class C:
            def connect(self, host, port):
                if not srv.online:
                    raise OSError("NAS nicht erreichbar")
                self.alive = True

            def login(self, u, p):
                srv.logins.append((u, p))

            def prot_p(self): pass
            def voidcmd(self, c): pass

            def mkd(self, d):
                if d in srv.dirs:
                    raise ftplib.error_perm("550 exists")
                srv.dirs.add(d)

            def size(self, path):
                self._check()
                if path not in srv.files:
                    raise ftplib.error_perm("550 not found")
                return len(srv.files[path])

            def delete(self, path):
                self._check()
                if path not in srv.files:
                    raise ftplib.error_perm("550 not found")
                del srv.files[path]

            def rename(self, a, b):
                self._check()
                srv.files[b] = srv.files.pop(a)

            def storbinary(self, cmd, fp):
                self._check()
                if srv.drop_next_write:
                    srv.drop_next_write -= 1
                    self.alive = False
                    raise ftplib.error_temp("421 Timeout")
                path = cmd.split(" ", 1)[1]
                srv.files[path] = srv.files.get(path, b"") + fp.read()

            def _check(self):
                if not srv.online or not self.alive:
                    raise OSError("Verbindung getrennt")

            def quit(self): self.alive = False
            def close(self): self.alive = False
        return C()

    def text(self, name="heizstab.log"):
        return self.files.get(f"/NAS-Nacht/EVCC-Logging/{name}", b"").decode("utf-8")


FTPS_CFG = {"log_nas": {"protokoll": "ftps", "server": "10.10.10.2", "port": 21,
                        "ordner": "/NAS-Nacht/EVCC-Logging", "datei": "heizstab.log",
                        "benutzer": "Server", "passwort": "geheim",
                        "neuer_versuch_sekunden": 0.2}}


@pytest.fixture
def ftp(monkeypatch):
    srv = FakeFtpServer()
    monkeypatch.setattr(control.ftplib, "FTP_TLS", srv.client)
    yield srv
    if control._log_listener:
        control._log_listener.stop()
        control._log_listener = None
    for h in list(control.logger.handlers):
        control.logger.removeHandler(h)


def test_ftps_schreibt_und_legt_ordner_an(ftp):
    control.setup_logging(FTPS_CFG)
    control.log("Stufe 3 ? Verz?gerung 0 s")
    flush()
    assert "Stufe 3 ? Verz?gerung 0 s" in ftp.text()
    assert ftp.logins[0] == ("Server", "geheim")
    assert "/NAS-Nacht/EVCC-Logging" in ftp.dirs


def test_ftps_rotation(ftp):
    control.setup_logging({**FTPS_CFG, "log_max_mb": 0.01, "log_anzahl_dateien": 2})
    for i in range(1000):
        control.log(f"Zeile {i} " + "x" * 50)
    flush()
    assert sorted(ftp.files) == [f"/NAS-Nacht/EVCC-Logging/heizstab.log{s}" for s in ("", ".1", ".2")]
    assert all(len(v) <= int(0.01 * 1024 * 1024) for v in ftp.files.values())
    assert "Zeile 999 " in ftp.text()


def test_ftps_trennung_nach_leerlauf_verliert_keine_zeile(ftp):
    control.setup_logging(FTPS_CFG)
    control.log("erste")
    flush()
    ftp.drop_next_write = 1
    control.log("nach Leerlauf")
    flush()
    assert "nach Leerlauf" in ftp.text()


def test_ftps_offline_dann_wieder_da(ftp):
    ftp.online = False
    control.setup_logging(FTPS_CFG)
    control.log("verloren")
    flush()
    assert ftp.files == {}
    ftp.online = True
    time.sleep(0.3)
    control.log("wieder da")
    flush()
    assert "wieder da" in ftp.text() and "verloren" not in ftp.text()


def test_ftps_ohne_smbprotocol(ftp, monkeypatch):
    monkeypatch.setattr(control, "smbclient", None)
    assert control.setup_logging(FTPS_CFG) is not None
