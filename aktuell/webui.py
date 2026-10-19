"""
Kleine Weboberflaeche zum Bearbeiten der config.json.

Aufruf: http://<pi>:5000/config

- Formular mit allen Einstellungen, gruppiert, mit Pruefung der Eingaben.
- Passwoerter/Tokens werden nie angezeigt; leeres Feld = unveraendert lassen.
- Speichern ist atomar (keine halb geschriebene Datei), die vorige Version bleibt
  als config.json.bak erhalten. Unbekannte Eintraege in der Datei bleiben erhalten.
- Schutz per Benutzer/Passwort (web_benutzer / web_passwort) und CSRF-Token.
"""
import hmac
import json
import os
import secrets
import time

from flask import Response, redirect, render_template_string, request


def F(key, label, typ="str", default="", help="", min=None, max=None, options=None):
    return dict(key=key, label=label, type=typ, default=default, help=help,
                min=min, max=max, options=options)


SECTIONS = [
    ("ESP / Relais", [
        F("esp_heizstab_ip", "IP-Adresse des ESP", "host", "10.0.10.98"),
        F("esp_user", "Tasmota-Benutzer", "str", "admin"),
        F("esp_password", "Tasmota-Passwort", "secret"),
        F("esp_timeout", "Timeout pro Anfrage (s)", "float", 3, min=0.5, max=30),
        F("esp_versuche", "Versuche beim Schalten", "int", 3, min=1, max=10),
    ]),
    ("Stufen", [
        F("relais_stufe_1", "Relais Stufe 1", "relays", [4], "z. B. 4"),
        F("relais_stufe_2", "Relais Stufe 2", "relays", [3, 4], "z. B. 3, 4"),
        F("relais_stufe_3", "Relais Stufe 3", "relays", [2, 3, 4], "z. B. 2, 3, 4"),
        F("leistung_pro_stufe", "Leistung pro Stufe (W)", "int", 1000, min=1, max=20000),
        F("spannung", "Netzspannung (V)", "int", 230, min=100, max=400,
          help="evcc rechnet: Ampere x Spannung x Phasen = Watt"),
        F("phasen", "Phasen", "int", 3, min=1, max=3),
    ]),
    ("Schaltschwellen", [
        F("puffer_einschalten", "Einschalt-Puffer (W)", "int", 200, min=0, max=5000,
          help="Stufe N schaltet ein ab N x Leistung pro Stufe + Puffer"),
        F("abschalt_unter_s1", "Stufe 1 aus unter (W)", "int", 1000, min=0, max=60000),
        F("abschalt_unter_s2", "Stufe 2 aus unter (W)", "int", 2000, min=0, max=60000),
        F("abschalt_unter_s3", "Stufe 3 aus unter (W)", "int", 3000, min=0, max=60000),
    ]),
    ("Zeiten", [
        F("zeit_bis_zuschalten", "Verzoegerung hochschalten (s)", "int", 60, min=0, max=86400),
        F("zeit_bis_abschalten", "Verzoegerung runterschalten (s)", "int", 10, min=0, max=86400),
        F("mindest_laufzeit", "Mindestlaufzeit pro Stufe (s)", "int", 300, min=0, max=86400),
        F("evcc_timeout_seconds", "Watchdog: aus nach (s) ohne evcc", "int", 300, min=10, max=86400),
    ]),
    ("Temperatur", [
        F("use_home_assistant", "Temperatur aus Home Assistant", "bool", True,
          help="sonst direkt vom Tasmota THR316D"),
        F("ha_url", "Home-Assistant-URL", "str", "http://10.10.10.2:8123"),
        F("ha_token", "Home-Assistant-Token", "secret"),
        F("ha_sensor_entity", "Sensor-Entity", "str", ""),
        F("ha_timeout", "Timeout (s)", "float", 5, min=1, max=60),
        F("tasmota_thr316d_ip", "IP des THR316D", "str", ""),
    ]),
    ("Logging", [
        F("log_level", "Log-Level", "select", "INFO", options=["DEBUG", "INFO", "WARNING", "ERROR"],
          help="DEBUG schreibt jede evcc-Anfrage mit"),
        F("log_max_mb", "Max. Groesse pro Datei (MB)", "float", 1, min=0.05, max=100),
        F("log_anzahl_dateien", "Alte Dateien behalten", "int", 3, min=0, max=50),
        F("log_nas.protokoll", "Protokoll", "select", "ftps", options=["ftps", "smb"]),
        F("log_nas.server", "NAS-Adresse", "str", "", help="leer = kein Logging"),
        F("log_nas.port", "Port (FTPS)", "int", 21, min=1, max=65535),
        F("log_nas.freigabe", "Freigabe (nur SMB)", "str", ""),
        F("log_nas.ordner", "Ordner", "str", "/NAS-Nacht/EVCC-Logging"),
        F("log_nas.datei", "Dateiname", "str", "heizstab.log"),
        F("log_nas.benutzer", "NAS-Benutzer", "str", ""),
        F("log_nas.passwort", "NAS-Passwort", "secret"),
        F("log_nas.neuer_versuch_sekunden", "Neuer Versuch nach (s)", "int", 300, min=10, max=86400),
    ]),
    ("Weboberflaeche", [
        F("web_benutzer", "Benutzer", "str", "admin"),
        F("web_passwort", "Passwort", "secret", help="leer lassen = unveraendert"),
    ]),
]
FIELDS = [f for _, fs in SECTIONS for f in fs]


def get_path(cfg, key, default=None):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(cfg, key, value):
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def display_value(f, cfg):
    v = get_path(cfg, f["key"], f["default"])
    if f["type"] == "relays":
        return ", ".join(str(x) for x in (v or []))
    return v


def parse_number(raw, f, errors):
    txt = raw.strip().replace(",", ".")
    try:
        val = int(txt) if f["type"] == "int" else float(txt)
    except ValueError:
        errors[f["key"]] = "keine gueltige Zahl" + (" (ganze Zahl)" if f["type"] == "int" else "")
        return None
    if f["min"] is not None and val < f["min"] or f["max"] is not None and val > f["max"]:
        errors[f["key"]] = f"erlaubt: {f['min']} bis {f['max']}"
        return None
    return val


def parse_form(form, old_cfg):
    """Formular -> neue Konfiguration. Rueckgabe (neue_config, fehler_dict)."""
    new = json.loads(json.dumps(old_cfg))  # tiefe Kopie, unbekannte Keys bleiben
    errors = {}
    for f in FIELDS:
        key, typ = f["key"], f["type"]
        raw = form.get(key, "")

        def set_path(cfg, k, value, _f=f):
            # Fehlt der Eintrag in der Datei und bleibt er beim Standardwert,
            # wird er nicht neu hineingeschrieben.
            if get_path(old_cfg, k) is None and value == _f["default"]:
                return
            _set_path(cfg, k, value)
        if typ == "bool":
            set_path(new, key, key in form)
        elif typ == "secret":
            if raw != "":
                set_path(new, key, raw)
        elif typ in ("int", "float"):
            val = parse_number(raw, f, errors)
            if val is not None:
                set_path(new, key, val)
        elif typ == "relays":
            try:
                relays = sorted({int(x) for x in raw.replace(";", ",").replace(" ", ",").split(",") if x.strip()})
            except ValueError:
                errors[key] = "nur Relais-Nummern, z. B. 2, 3, 4"
                continue
            if not relays:
                errors[key] = "mindestens ein Relais"
            elif any(r < 1 or r > 8 for r in relays):
                errors[key] = "Relais-Nummern 1 bis 8"
            else:
                set_path(new, key, relays)
        elif typ == "select":
            if raw not in f["options"]:
                errors[key] = "ungueltige Auswahl"
            else:
                set_path(new, key, raw)
        elif typ == "host":
            if not raw.strip():
                errors[key] = "darf nicht leer sein"
            else:
                set_path(new, key, raw.strip())
        else:
            set_path(new, key, raw.strip())

    # Hysterese: Abschaltschwelle muss unter der Einschaltschwelle liegen, sonst flattert es
    if not errors:
        p = new.get("puffer_einschalten", 200)
        w = new.get("leistung_pro_stufe", 1000)
        for n in (1, 2, 3):
            ein, aus = n * w + p, new.get(f"abschalt_unter_s{n}", n * 1000)
            if aus >= ein:
                errors[f"abschalt_unter_s{n}"] = f"muss kleiner als die Einschaltschwelle ({ein} W) sein"
    return new, errors


def save_atomic(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    os.replace(tmp, path)


def changed_keys(old, new):
    keys = []
    for f in FIELDS:
        if get_path(old, f["key"], f["default"]) != get_path(new, f["key"], f["default"]):
            keys.append(f["key"])
    return keys


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heizstab - Einstellungen</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:0 auto;padding:12px;background:#f4f5f7;color:#222}
 h1{font-size:1.4em} fieldset{background:#fff;border:1px solid #ddd;border-radius:8px;margin:0 0 14px;padding:10px 14px}
 legend{font-weight:600;padding:0 6px} label{display:block;margin:8px 0 2px;font-size:.92em}
 input[type=text],input[type=password],select{width:100%;box-sizing:border-box;padding:7px;border:1px solid #bbb;border-radius:5px;font-size:1em}
 .help{font-size:.8em;color:#666} .err{color:#b00020;font-size:.85em} .bad{border-color:#b00020!important}
 .msg{padding:10px;border-radius:6px;margin-bottom:12px} .ok{background:#e3f5e6} .warn{background:#fff3cd} .fail{background:#fde2e4}
 .status{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:.92em}
 button{background:#1a73e8;color:#fff;border:0;border-radius:6px;padding:11px 22px;font-size:1em;cursor:pointer}
 .bar{position:sticky;bottom:0;background:#f4f5f7;padding:10px 0}
 .calc p{margin:6px 0} .big{font-size:1.15em}
 table{border-collapse:collapse;width:100%;font-size:.9em;margin-top:8px}
 th,td{border-bottom:1px solid #eee;padding:5px 4px;text-align:right} th:first-child,td:first-child{text-align:left}
 tr.cur{background:#e8f0fe;font-weight:600} tr.tgt td:first-child::after{content:" ← Ziel";color:#1a73e8;font-weight:600}
 .try{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
 .try input{width:90px;padding:7px;border:1px solid #bbb;border-radius:5px;font-size:1em}
 .try button{padding:7px 14px}
</style></head><body>
<h1>Heizstab-Steuerung</h1>
{% if not has_password %}<div class="msg warn">Kein Passwort fuer die Weboberflaeche gesetzt - jeder im Netzwerk kann die Einstellungen aendern. Bitte unten unter <b>Weboberflaeche</b> ein Passwort vergeben.</div>{% endif %}
{% if saved %}<div class="msg ok">Gespeichert{% if changed %}: {{ changed|join(', ') }}{% else %} (keine Aenderung){% endif %}. Aenderungen wirken sofort.</div>{% endif %}
{% if errors %}<div class="msg fail">Nicht gespeichert - bitte die markierten Felder pruefen.</div>{% endif %}
<fieldset><legend>Aktueller Zustand</legend><div class="status">
 {% for k, v in status.items() %}<span>{{ k }}: <b>{{ v }}</b></span>{% endfor %}
</div></fieldset>
<fieldset class="calc"><legend>Leistungsberechnung</legend>
 <div id="calc">wird geladen ...</div>
 <div class="try"><span>Was waere bei</span><input id="tryA" type="text" inputmode="decimal" placeholder="z. B. 3">
  <span>A?</span><button type="button" id="tryBtn">Berechnen</button></div>
 <p id="tryOut" class="help"></p>
 <p class="help">Berechnet mit den <b>gespeicherten</b> Einstellungen, aktualisiert sich alle 5 s.</p>
</fieldset>
<script>
const nf = (n, d) => Number(n).toLocaleString('de-DE', {minimumFractionDigits: d||0, maximumFractionDigits: d||0});
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function getCalc(a) {
  const r = await fetch('/config/rechnung' + (a !== undefined ? '?ampere=' + encodeURIComponent(a) : ''));
  if (!r.ok) throw new Error((await r.json()).fehler || r.status);
  return r.json();
}
function rows(d) {
  return d.stufen.map(s => `<tr class="${s.stufe===d.aktuelle_stufe?'cur':''} ${s.stufe===d.ziel_stufe&&d.ziel_stufe!==d.aktuelle_stufe?'tgt':''}">
    <td>Stufe ${s.stufe}</td><td>${esc(s.relais.join(', '))}</td><td>${nf(s.leistung)} W</td>
    <td>${nf(s.ein_w)} W<br><span class="help">${nf(s.ein_a,2)} A</span></td>
    <td>${nf(s.aus_w)} W<br><span class="help">${nf(s.aus_a,2)} A</span></td></tr>`).join('');
}
async function refresh() {
  try {
    const d = await getCalc();
    document.getElementById('calc').innerHTML = `
     <p class="big">evcc meldet <b>${nf(d.ampere,1)} A</b> &times; ${d.spannung} V &times; ${d.phasen} = <b>${nf(d.watt)} W</b></p>
     <p>&rarr; Zielstufe <b>${d.ziel_stufe}</b> (${nf(d.leistung_ziel)} W) &middot; aktuell geschaltet: Stufe <b>${d.aktuelle_stufe}</b> (${nf(d.leistung_aktuell)} W)</p>
     <p><b>${esc(d.grund)}</b></p>
     <table><tr><th>Stufe</th><th>Relais</th><th>Heizleistung</th><th>einschalten ab</th><th>ausschalten unter</th></tr>${rows(d)}</table>
     <p class="help">Damit Stufe 1 einschaltet, muss evcc mindestens <b>${nf(d.min_ampere_stufe1_genau,2)} A</b> senden
      &ndash; bei ganzen Ampere also <b>${d.min_ampere_stufe1} A</b> (<code>mincurrent</code> in der evcc.yaml).</p>`;
  } catch (e) { document.getElementById('calc').textContent = 'Berechnung nicht verfuegbar: ' + e.message; }
}
async function tryIt() {
  const out = document.getElementById('tryOut');
  try {
    const d = await getCalc(document.getElementById('tryA').value);
    out.innerHTML = `${nf(d.ampere,1)} A = <b>${nf(d.watt)} W</b> &rarr; Stufe <b>${d.ziel_stufe}</b> (${nf(d.leistung_ziel)} W Heizleistung)`
      + (d.aktuelle_stufe ? ` &ndash; ausgehend von der aktuellen Stufe ${d.aktuelle_stufe} (Hysterese)` : '')
      + (d.freigabe ? '' : ' &ndash; nur wenn evcc freigibt');
  } catch (e) { out.textContent = e.message; }
}
document.getElementById('tryBtn').onclick = tryIt;
document.getElementById('tryA').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); tryIt(); } };
refresh(); setInterval(refresh, 5000);
</script>
<form method="post" action="/config">
<input type="hidden" name="_csrf" value="{{ csrf }}">
{% for title, fields in sections %}<fieldset><legend>{{ title }}</legend>
 {% for f in fields %}
  {% if f.type == 'bool' %}
   <label><input type="checkbox" name="{{ f.key }}" {% if values[f.key] %}checked{% endif %}> {{ f.label }}</label>
  {% else %}
   <label for="{{ f.key }}">{{ f.label }}</label>
   {% if f.type == 'select' %}
    <select id="{{ f.key }}" name="{{ f.key }}">{% for o in f.options %}<option {% if o == values[f.key] %}selected{% endif %}>{{ o }}</option>{% endfor %}</select>
   {% elif f.type == 'secret' %}
    <input type="password" id="{{ f.key }}" name="{{ f.key }}" value="" autocomplete="new-password"
           placeholder="{{ 'gesetzt - leer lassen = unveraendert' if values[f.key] else 'nicht gesetzt' }}" class="{{ 'bad' if f.key in errors }}">
   {% else %}
    <input type="text" id="{{ f.key }}" name="{{ f.key }}" value="{{ values[f.key] }}" class="{{ 'bad' if f.key in errors }}">
   {% endif %}
  {% endif %}
  {% if f.key in errors %}<div class="err">{{ errors[f.key] }}</div>{% elif f.help %}<div class="help">{{ f.help }}</div>{% endif %}
 {% endfor %}
</fieldset>{% endfor %}
<div class="bar"><button type="submit">Speichern</button></div>
</form></body></html>"""


def register(app, config_file, load_config, on_saved=None, get_status=None,
             get_calculation=None, log=print):
    """Haengt die Weboberflaeche an die bestehende Flask-App."""
    csrf_token = secrets.token_urlsafe(24)

    def check_auth():
        cfg = load_config()
        pw = cfg.get("web_passwort", "")
        if not pw:
            return True
        auth = request.authorization
        return bool(auth) and hmac.compare_digest(auth.username or "", cfg.get("web_benutzer", "admin")) \
            and hmac.compare_digest(auth.password or "", pw)

    def deny():
        return Response("Anmeldung erforderlich", 401,
                        {"WWW-Authenticate": 'Basic realm="Heizstab", charset="UTF-8"'})

    def render(cfg, values=None, errors=None, saved=False, changed=None):
        if values is None:
            values = {f["key"]: display_value(f, cfg) for f in FIELDS}
            for f in FIELDS:
                if f["type"] == "secret":
                    values[f["key"]] = bool(get_path(cfg, f["key"]))
        return render_template_string(
            PAGE, sections=SECTIONS, values=values, errors=errors or {}, saved=saved,
            changed=changed or [], csrf=csrf_token, has_password=bool(cfg.get("web_passwort")),
            status=get_status() if get_status else {})

    @app.route("/", methods=["GET"])
    def webui_root():
        return redirect("/config")

    @app.route("/config", methods=["GET"])
    def webui_get():
        if not check_auth():
            return deny()
        return render(load_config(), saved=request.args.get("saved") == "1",
                      changed=[c for c in request.args.get("changed", "").split(",") if c])

    @app.route("/config/rechnung", methods=["GET"])
    def webui_calc():
        if not check_auth():
            return deny()
        if not get_calculation:
            return {"fehler": "Berechnung nicht verfuegbar"}, 404
        raw = request.args.get("ampere")
        if raw is None:
            return get_calculation()
        try:
            amps = float(raw.strip().replace(",", "."))
        except ValueError:
            return {"fehler": "Bitte eine Zahl eingeben, z. B. 3 oder 2,5"}, 400
        if not 0 <= amps <= 100:
            return {"fehler": "Bitte einen Wert zwischen 0 und 100 A eingeben"}, 400
        return get_calculation(amps)

    @app.route("/config", methods=["POST"])
    def webui_post():
        if not check_auth():
            return deny()
        if not hmac.compare_digest(request.form.get("_csrf", ""), csrf_token):
            return Response("Formular abgelaufen - Seite neu laden und nochmal speichern.", 400)
        old = load_config()
        new, errors = parse_form(request.form, old)
        if errors:
            values = {f["key"]: request.form.get(f["key"], "") for f in FIELDS}
            for f in FIELDS:
                if f["type"] == "bool":
                    values[f["key"]] = f["key"] in request.form
                elif f["type"] == "secret":
                    values[f["key"]] = bool(get_path(old, f["key"]))
            return render(old, values=values, errors=errors), 400
        changed = changed_keys(old, new)
        if changed:
            save_atomic(config_file, new)
            log(f"Konfiguration ueber Weboberflaeche geaendert: {', '.join(changed)} "
                f"(von {request.remote_addr})")
            if on_saved:
                on_saved(old, new, changed)
        # Redirect nach POST: Neuladen der Seite speichert nicht erneut
        return redirect(f"/config?saved=1&changed={','.join(changed)}&t={int(time.time())}", 303)
