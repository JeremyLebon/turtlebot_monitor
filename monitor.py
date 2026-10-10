#!/usr/bin/env python3
"""TurtleBot fleet monitor - draait op de laptop van de docent.

Elke robot stuurt elke 5 s zijn status (system_info_node, MONITOR_URL) naar
deze server; de laptop zelf kan de robots niet bereiken (elke robot zit achter
de NAT van zijn eigen AP). Enkel de Python-standaardbibliotheek: werkt op
Windows, in WSL en op Linux.

    python monitor.py              # http://localhost:8090
    python monitor.py --port 9000
    python monitor.py --remote-control   # knoppen ook vanaf andere toestellen
    python monitor.py --view-only        # enkel kijken, geen knoppen

Knoppen (stop, piep, bringup, bijwerken, uitschakelen, ...): de robot vraagt
zelf om opdrachten (long-poll op /poll) - ook dat werkt door de NAT. De knoppen
werken standaard enkel vanaf deze laptop (localhost): een student die vanaf
een robot-AP de monitor opent, kan niets bedienen.

De robots moeten deze laptop kunnen bereiken: zet het adres in
turtlebot_setup/monitor.conf (MONITOR_URL=http://<ip-van-de-laptop>:8090/push)
en geef de laptop een vast IP op het switch-subnet. Windows: sta Python toe
in de firewall (privénetwerk) wanneer daarom gevraagd wordt.
"""
import argparse
import base64
import csv
import glob
import io
import json
import os
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_CSV = os.path.join(HERE, "..", "turtlebot_config.csv")
# Kaartenbibliotheek: kaartbundels (kaart + zones + routes) van de robots,
# om op andere robots te zetten. Bestanden: monitor/maps/<naam>.tbmap.json
MAP_LIB = os.path.join(HERE, "maps")
MAP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def library():
    out = []
    for path in sorted(glob.glob(os.path.join(MAP_LIB, "*.tbmap.json"))):
        try:
            with open(path) as f:
                b = json.load(f)
            out.append({"name": os.path.basename(path)[:-len(".tbmap.json")], "from": b.get("from"),
                        "created": b.get("created"), "zones": len(b.get("zones") or []),
                        "routes": len(b.get("routes") or {}), "size_kb": round(os.path.getsize(path) / 1024)})
        except (OSError, ValueError):
            continue
    return out


def bundle_from_zip(data):
    """Zip from nav.html "Exporteer" (<kaart>.yaml + .pgm, zones, routes) -> bundle."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        yaml_name = next(n for n in names if n.endswith(".yaml"))
        yaml_text = z.read(yaml_name).decode()
        m = re.search(r"(?m)^image:\s*(\S+)", yaml_text)
        img = os.path.join(os.path.dirname(yaml_name), os.path.basename(m.group(1))) if m else yaml_name[:-5] + ".pgm"
        stem = yaml_name[:-5]

        def opt(suffix, key, default):
            return json.loads(z.read(stem + suffix)).get(key, default) if stem + suffix in names else default
        return {"format": "turtlebot-map/1", "name": os.path.basename(stem), "from": "zip", "created": time.time(),
                "yaml": yaml_text, "pgm_b64": base64.b64encode(z.read(img)).decode(),
                "zones": opt(".zones.json", "zones", []), "routes": opt(".routes.json", "routes", {})}


def store_map(name, bundle):
    if not MAP_NAME_RE.match(name) or bundle.get("format") != "turtlebot-map/1":
        raise ValueError("geen geldige kaartbundel")
    os.makedirs(MAP_LIB, exist_ok=True)
    path = os.path.join(MAP_LIB, name + ".tbmap.json")
    with open(path + ".tmp", "w") as f:
        json.dump(bundle, f)
    os.replace(path + ".tmp", path)

robots = {}          # hostname -> {"data": payload, "seen": time, "addr": ip}
lock = threading.Lock()
pending = {}         # hostname -> [command, ...] waiting for the robot's poll
polling = {}         # hostname -> time of the last poll (robot listens)
results = {}         # hostname -> last result {"cmd", "ok", "message", "t"}
cond = threading.Condition()
cmd_seq = [0]
REMOTE_CONTROL = False
VIEW_ONLY = False
ROBOT_COMMANDS = {"stop_all", "beep", "update", "reboot", "shutdown", "map_upload", "map_download"} | {
    a + ":" + n for a in ("start", "stop") for n in ("bringup", "slam", "navigation", "camera", "joystick")}


def queue_command(names, cmd, arg=None):
    with cond:
        for name in names:
            cmd_seq[0] += 1
            c = {"id": cmd_seq[0], "cmd": cmd, "t": time.time()}
            if arg is not None:
                c["arg"] = arg
            pending.setdefault(name, []).append(c)
            results[name] = {"cmd": cmd, "ok": None, "message": "verstuurd...", "t": time.time()}
        cond.notify_all()


# "Alles bijwerken": robots in groepjes van <parallel> bijwerken i.p.v. alle
# tegelijk (internet van de switch + container-herstart midden in de les).
# Een robot is klaar als zijn update-service niet meer "activating" is
# (launch.update, robots vanaf image 2026-10-10); oudere robots sturen dat
# niet mee: dan klaar na offline->online (container herstart) of na 6 min.
UPDATE_TIMEOUT = 35 * 60        # unit: TimeoutStartSec=30min
UPDATE_FALLBACK = 6 * 60
upd = {"waiting": [], "running": {}, "done": {}, "parallel": 2, "started": None}


def update_queue_start(names, parallel):
    with cond:
        busy = set(upd["waiting"]) | set(upd["running"])
        if not busy:
            upd["done"] = {}
            upd["started"] = time.time()
        upd["parallel"] = max(1, min(int(parallel), 9))
        upd["waiting"] += [n for n in names if n not in busy]


def update_queue_tick():
    now = time.time()
    with lock:
        seen = dict(robots)
    with cond:
        for name, r in list(upd["running"].items()):
            info = seen.get(name, {})
            online = now - info.get("seen", 0) < 20
            res = results.get(name) or {}
            u = ((info.get("data") or {}).get("launch") or {}).get("update")
            done = None
            if res.get("cmd") == "update" and res.get("ok") is False:
                done = (False, res.get("message") or "update niet gestart")
            elif u:
                r["info"] = True
                if u.get("state") == "activating":
                    r["active"] = True
                elif u.get("finished") and u["finished"] > r["t"] - 5:
                    ok = u.get("state") == "active" and u.get("result") == "success"
                    done = (ok, "OK" if ok else "mislukt: " + str(u.get("message") or u.get("result")))
            elif not r.get("info"):
                if not online:
                    r["went_off"] = True
                elif r.get("went_off") or now - r["t"] > UPDATE_FALLBACK:
                    done = (True, "vermoedelijk OK (oude software meldt geen updatestatus)")
            if not done and now - r["t"] > UPDATE_TIMEOUT:
                done = (False, "time-out")
            if not done and not online and now - info.get("seen", 0) > 600:
                done = (False, "robot al 10 min offline")
            if done:
                upd["done"][name] = {"ok": done[0], "message": done[1], "t": now}
                del upd["running"][name]
        while upd["waiting"] and len(upd["running"]) < upd["parallel"]:
            name = upd["waiting"].pop(0)
            if now - polling.get(name, 0) > 30:
                upd["done"][name] = {"ok": False, "message": "luisterde niet naar de monitor - overgeslagen", "t": now}
                continue
            upd["running"][name] = {"t": now}
            cmd_seq[0] += 1
            pending.setdefault(name, []).append({"id": cmd_seq[0], "cmd": "update", "t": now})
            results[name] = {"cmd": "update", "ok": None, "message": "verstuurd...", "t": now}
            cond.notify_all()


def update_queue_loop():
    while True:
        try:
            update_queue_tick()
        except Exception as exc:     # never let the queue thread die
            print("update-wachtrij:", exc)
        time.sleep(3)


def update_queue_state():
    with cond:
        return {"waiting": list(upd["waiting"]), "running": sorted(upd["running"]),
                "done": dict(upd["done"]), "parallel": upd["parallel"], "started": upd["started"]}


def expected_robots():
    """Robots from turtlebot_config.csv, so never-seen robots show up too."""
    try:
        with open(CONFIG_CSV, newline="") as f:
            return [{"robot": r["hostname"], "nr": r["nr"]} for r in csv.DictReader(f)]
    except (OSError, KeyError):
        return []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # quiet: 9 robots x every 5 s

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _may_control(self):
        # control server (--control-port, Docker: published on 127.0.0.1 only),
        # or a browser on this laptop itself
        return not VIEW_ONLY and (getattr(self.server, "control", False) or REMOTE_CONTROL
                                  or self.client_address[0] in ("127.0.0.1", "::1"))

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(min(n, 20 * 1024 * 1024)))   # map bundles: some 100 kB

    def do_POST(self):
        if self.path == "/result":     # robot: result of a command
            try:
                r = self._read_json()
                name = str(r["robot"])[:40]
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            with cond:
                results[name] = {"cmd": r.get("cmd"), "ok": r.get("ok"),
                                 "message": str(r.get("message", ""))[:300], "t": time.time()}
            return self._send(200, "ok", "text/plain")
        if self.path.startswith("/maps/"):   # robot uploads a map bundle
            name = self.path[len("/maps/"):]
            try:
                store_map(name, self._read_json())
            except (ValueError, TypeError, OSError) as exc:
                return self._send(400, str(exc), "text/plain")
            return self._send(200, "ok", "text/plain")
        if self.path in ("/api/maps/delete", "/api/maps/upload") and not self._may_control():
            return self._send(403, json.dumps({"error": "enkel vanaf de laptop zelf"}), "application/json")
        if self.path == "/api/maps/delete":
            try:
                name = self._read_json()["name"]
                if not MAP_NAME_RE.match(name):
                    raise ValueError
                os.remove(os.path.join(MAP_LIB, name + ".tbmap.json"))
            except (ValueError, KeyError, TypeError, OSError):
                return self._send(400, "bad request", "text/plain")
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if self.path == "/api/maps/upload":   # zip (nav.html "Exporteer") or .tbmap.json from the laptop
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(min(n, 20 * 1024 * 1024))
                b = bundle_from_zip(data) if data[:2] == b"PK" else json.loads(data)
                store_map(str(b.get("name", "")), b)
            except (ValueError, TypeError, OSError, KeyError, StopIteration, zipfile.BadZipFile) as exc:
                return self._send(400, json.dumps({"error": str(exc) or "geen geldige kaartbundel"}), "application/json")
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if self.path in ("/api/update_all", "/api/update_all/cancel"):
            if not self._may_control():
                return self._send(403, json.dumps({"error": "knoppen enkel vanaf de laptop zelf (localhost)"}),
                                  "application/json")
            if self.path.endswith("/cancel"):
                with cond:
                    upd["waiting"] = []    # running updates finish by themselves
                return self._send(200, json.dumps({"ok": True}), "application/json")
            try:
                r = self._read_json()
                names = [str(n)[:40] for n in r["robots"]]
                update_queue_start(names, r.get("parallel", 2))
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            return self._send(200, json.dumps({"queued": len(names)}), "application/json")
        if self.path == "/api/cmd":    # button on the page
            if VIEW_ONLY:
                return self._send(403, json.dumps({"error": "monitor gestart met --view-only"}), "application/json")
            if not self._may_control():
                return self._send(403, json.dumps({"error": "knoppen enkel vanaf de laptop zelf (localhost)"}),
                                  "application/json")
            try:
                r = self._read_json()
                cmd, names = r["cmd"], r["robots"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            if cmd not in ROBOT_COMMANDS or not isinstance(names, list):
                return self._send(400, "onbekende opdracht", "text/plain")
            arg = r.get("arg")
            if cmd.startswith("map_") and not MAP_NAME_RE.match(str(arg or "")):
                return self._send(400, "ongeldige kaartnaam", "text/plain")
            queue_command([str(n)[:40] for n in names], cmd, arg)
            return self._send(200, json.dumps({"queued": len(names)}), "application/json")
        if self.path != "/push":
            return self._send(404, "not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(min(n, 100000)))
            name = str(payload["robot"])[:40]
        except (ValueError, KeyError, TypeError):
            return self._send(400, "bad request", "text/plain")
        with lock:
            robots[name] = {"data": payload, "seen": time.time(), "addr": self.client_address[0]}
        self._send(200, "ok", "text/plain")

    def do_GET(self):
        if self.path.startswith("/poll?"):     # robot waits for commands (long-poll)
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("robot", [""])[0][:40]
            deadline = time.time() + 20
            with cond:
                polling[name] = time.time()
                while not pending.get(name) and time.time() < deadline:
                    cond.wait(deadline - time.time())
                cmds = pending.pop(name, [])
                polling[name] = time.time()
            # commands older than 60 s (robot was away) are dropped
            cmds = [c for c in cmds if time.time() - c["t"] < 60]
            return self._send(200, json.dumps({"cmds": cmds}), "application/json")
        if self.path.startswith("/maps/"):    # robot downloads a map bundle
            name = self.path[len("/maps/"):]
            path = os.path.join(MAP_LIB, name + ".tbmap.json")
            if not MAP_NAME_RE.match(name) or not os.path.exists(path):
                return self._send(404, "kaart niet in de bibliotheek", "text/plain")
            with open(path, "rb") as f:
                return self._send(200, f.read(), "application/json")
        if self.path == "/api/maps":
            return self._send(200, json.dumps(library()), "application/json")
        if self.path == "/api/robots":
            with lock:
                seen = dict(robots)
            out = []
            names = set()
            for r in expected_robots():
                names.add(r["robot"])
                out.append(dict(r, **seen.get(r["robot"], {})))
            for name, r in seen.items():
                if name not in names:
                    out.append(dict({"robot": name, "nr": r["data"].get("nr")}, **r))
            with cond:
                for r in out:
                    r["result"] = results.get(r["robot"])
                    r["listening"] = time.time() - polling.get(r["robot"], 0) < 30
            return self._send(200, json.dumps({"now": time.time(), "robots": out,
                                               "update_queue": update_queue_state(),
                                               "control": self._may_control(),
                                               "view_only": VIEW_ONLY}),
                              "application/json")
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        self._send(404, "not found", "text/plain")


PAGE = r"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TurtleBot Monitor</title>
<style>
:root { --bg:#14161a; --card:#1d2026; --border:#2e333b; --text:#e6e8eb; --muted:#8b939e;
        --green:#3fb950; --yellow:#e3b341; --red:#f85149; --accent:#58a6ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.4 system-ui,sans-serif; padding:16px; }
header { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
h1 { font-size:1.2rem; margin:0; }
.sub { color:var(--muted); font-size:.82rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
.tile { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:12px; border-left:5px solid var(--muted); }
.tile.ok { border-left-color:var(--green); } .tile.warn { border-left-color:var(--yellow); } .tile.bad { border-left-color:var(--red); }
.tile.off { opacity:.55; }
.name { font-weight:700; font-size:1rem; display:flex; justify-content:space-between; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; margin-top:8px; font-size:.85rem; }
.kv span:nth-child(odd) { color:var(--muted); }
.chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:8px; }
.chip { font-size:.72rem; padding:1px 8px; border-radius:999px; border:1px solid var(--border); color:var(--muted); }
.chip.on { background:var(--accent); border-color:var(--accent); color:#0b1a2b; font-weight:600; }
.alert { margin-top:8px; padding:4px 8px; border-radius:6px; background:var(--red); color:#2a0d0d; font-weight:600; font-size:.8rem; }
.warnmsg { margin-top:8px; padding:4px 8px; border-radius:6px; background:var(--yellow); color:#2a2000; font-size:.8rem; }
.g { color:var(--green); } .y { color:var(--yellow); } .r { color:var(--red); }
a { color:var(--accent); }
.bar { display:flex; flex-wrap:wrap; gap:6px; align-items:center; background:var(--card); border:1px solid var(--border);
       border-radius:12px; padding:8px 12px; margin-bottom:14px; }
button, select { background:#262a31; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:4px 10px;
                 font:inherit; font-size:.82rem; cursor:pointer; }
button:hover { border-color:var(--accent); }
button.stop { background:var(--red); border-color:var(--red); color:#2a0d0d; font-weight:700; }
button:disabled, select:disabled { opacity:.4; cursor:default; }
.btns { display:flex; flex-wrap:wrap; gap:4px; margin-top:8px; }
.res { margin-top:6px; font-size:.78rem; color:var(--muted); }
</style></head><body>
<header><h1>TurtleBot Monitor</h1><span class="sub" id="summary">laden...</span></header>
<div class="bar" id="allBar">
  <b>Alle robots:</b>
  <button class="stop" data-all="stop_all" title="Alle launches stoppen - motoren stil">STOP ALLES</button>
  <button data-all="start:bringup">Bringup starten</button>
  <button id="updAll" title="Software bijwerken (git + Docker-image), in groepjes zodat internet en lessen niet vastlopen">Alles bijwerken</button>
  <select id="updPar" title="Hoeveel robots tegelijk"><option value="1">1 tegelijk</option><option value="2" selected>2 tegelijk</option><option value="3">3 tegelijk</option></select>
  <button id="updCancel" style="display:none" title="Wachtende robots niet meer bijwerken (lopende updates werken af)">Wachtrij stoppen</button>
  <button data-all="shutdown" title="Alle robots uitschakelen (einde les)">Uitschakelen</button>
  <span class="sub" id="ctrlInfo"></span>
</div>
<div class="bar" id="updBox" style="display:none"></div>
<div class="grid" id="grid"></div>
<div class="bar" style="margin-top:14px;display:block" id="libBox">
  <b>Kaartenbibliotheek</b> <span class="sub">- kaart + zones + routes. Een robot stuurt een kaart hierheen via
  "meer... &gt; kaart X naar bibliotheek"; daarna kan ze naar andere robots. Bestaat de kaart daar al, dan wordt de oude
  eerst hernoemd naar &lt;naam&gt;_vorige.</span>
  <table style="width:100%;margin-top:8px;font-size:.85rem;border-collapse:collapse" id="libTable"></table>
  <div style="margin-top:8px"><label class="sub">Kaart van je laptop toevoegen (zip van "Exporteer" op nav.html):
    <input type="file" id="libFile" accept=".zip,.json"></label></div>
</div>
<p class="sub">Elke robot stuurt elke 5 s zijn status. Groen = alles in orde, geel = aandacht, rood = probleem, grijs = niets ontvangen.
Statuspagina per robot: enkel bereikbaar als je laptop op de wifi van die robot zit (TB-AP-&lt;nr&gt;).</p>
<script>
function esc(t) { return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
function ago(s) { return s < 60 ? Math.round(s) + " s" : s < 3600 ? Math.round(s / 60) + " min" : Math.round(s / 3600) + " u"; }
function batt(v) {   // 0 % = 11.0 V: there the OpenCR switches the motors off
  if (v == null) return ["--", ""];
  var p = Math.max(0, Math.min(100, (v - 11.0) / 1.3 * 100));
  return [v.toFixed(1) + " V (" + Math.round(p) + " %)", v < 11.2 ? "r" : v < 11.4 ? "y" : "g"];
}
var CMD_TEXT = { stop_all: "stop alles", beep: "piep", update: "bijwerken", reboot: "herstarten", shutdown: "uitschakelen",
                 map_upload: "kaart naar bibliotheek", map_download: "kaart uit bibliotheek" };
function cmdText(c) {
  if (CMD_TEXT[c]) return CMD_TEXT[c];
  var p = c.split(":"); return (p[0] === "start" ? "start " : "stop ") + p[1];
}
var CONFIRM = { reboot: 1, shutdown: 1, update: 1 };
var control = false;
function send(cmd, names, arg) {
  var many = names.length > 1;
  if ((CONFIRM[cmd] || many) && !confirm(cmdText(cmd) + (arg ? " '" + arg + "'" : "") + " voor " + (many ? names.length + " robots" : names[0]) + "?")) return;
  fetch("/api/cmd", { method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ cmd: cmd, robots: names, arg: arg }) })
    .then(function (x) { if (!x.ok) return x.json().then(function (j) { alert(j.error || "geweigerd"); }); refresh(); });
}
var lastRobots = [];
document.querySelectorAll("[data-all]").forEach(function (b) {
  b.addEventListener("click", function () {
    var names = lastRobots.filter(function (r) { return r.listening; }).map(function (r) { return r.robot; });
    if (!names.length) { alert("Geen enkele robot luistert naar de monitor."); return; }
    var cmd = b.dataset.all;
    if (cmd === "stop_all") {   // emergency: no confirm
      fetch("/api/cmd", { method: "POST", headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ cmd: cmd, robots: names }) }).then(refresh);
    } else send(cmd, names);
  });
});
function post(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then(function (x) { if (!x.ok) return x.json().then(function (j) { alert(j.error || "geweigerd"); }); refresh(); });
}
document.getElementById("updAll").addEventListener("click", function () {
  var names = lastRobots.filter(function (r) { return r.listening; }).map(function (r) { return r.robot; });
  if (!names.length) { alert("Geen enkele robot luistert naar de monitor."); return; }
  var par = document.getElementById("updPar").value;
  if (!confirm("Software bijwerken voor " + names.length + " robots, " + par + " tegelijk?\n" +
               "Bij een nieuwe image herstart de container: lopende launches stoppen.")) return;
  post("/api/update_all", { robots: names, parallel: Number(par) });
});
document.getElementById("updCancel").addEventListener("click", function () { post("/api/update_all/cancel"); });
function renderUpdate(q) {
  var box = document.getElementById("updBox");
  var done = Object.keys(q.done), busy = q.running.length + q.waiting.length;
  document.getElementById("updCancel").style.display = q.waiting.length ? "" : "none";
  if (!busy && !done.length) { box.style.display = "none"; return; }
  box.style.display = "";
  var ok = done.filter(function (n) { return q.done[n].ok; }).length;
  box.innerHTML = "<b>Bijwerken:</b> " + (busy ? "bezig" : "klaar") + " - " + ok + " OK, " + (done.length - ok) + " mislukt, " +
    q.running.length + " bezig, " + q.waiting.length + " wachtend" +
    (q.running.length ? ' <span class="sub">(nu: ' + esc(q.running.join(", ")) + ")</span>" : "") +
    done.filter(function (n) { return !q.done[n].ok; }).map(function (n) {
      return '<div class="r" style="width:100%;font-size:.8rem">' + esc(n) + ": " + esc(q.done[n].message) + "</div>";
    }).join("");
}
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-cmd]");
  if (b) send(b.dataset.cmd, [b.dataset.robot]);
});
document.addEventListener("change", function (e) {
  if (e.target.matches("select[data-robot]") && e.target.value) {
    var v = e.target.value.split("|");
    send(v[0], [e.target.dataset.robot], v[1]);
    e.target.value = "";
  }
  if (e.target.matches("select[data-libmap]") && e.target.value) {
    var target = e.target.value, name = e.target.dataset.libmap;
    var names = target === "*" ? lastRobots.filter(function (r) { return r.listening; }).map(function (r) { return r.robot; }) : [target];
    if (!names.length) alert("Geen enkele robot luistert naar de monitor.");
    else send("map_download", names, name);
    e.target.value = "";
  }
});
function controls(r) {
  var dis = control && r.listening ? "" : " disabled";
  var l = (r.data || {}).launch || {};
  var mapOpts = (l.maps || []).map(function (m) {
    return '<option value="map_upload|' + esc(m) + '">kaart ' + esc(m) + " naar bibliotheek</option>";
  }).join("");
  var opts = ["bringup", "slam", "navigation", "joystick", "camera"].map(function (k) {
    return '<option value="' + (l[k] ? "stop:" : "start:") + k + '">' + (l[k] ? "stop " : "start ") + k + "</option>";
  }).join("") + '<option value="update">bijwerken</option><option value="reboot">herstarten</option><option value="shutdown">uitschakelen</option>' + mapOpts;
  var res = r.result ? '<div class="res">' + esc(cmdText(r.result.cmd)) + ": " +
    (r.result.ok === null ? "verstuurd..." : (r.result.ok ? '<span class="g">OK</span> ' : '<span class="r">mislukt</span> ') + esc(r.result.message)) + "</div>" : "";
  return '<div class="btns"><button class="stop" data-cmd="stop_all" data-robot="' + esc(r.robot) + '"' + dis + ">Stop</button>" +
    '<button data-cmd="beep" data-robot="' + esc(r.robot) + '"' + dis + ' title="Laat de robot piepen, om hem terug te vinden">Piep</button>' +
    '<select data-robot="' + esc(r.robot) + '"' + dis + '><option value="">meer...</option>' + opts + "</select></div>" + res;
}
function tile(r, now) {
  var d = r.data || {}, s = d.status || {}, l = d.launch || {};
  var age = r.seen ? now - r.seen : null, online = age != null && age < 20;
  var level = "ok", msgs = [], warns = [];
  if (!online) level = "off";
  var b = batt(online ? d.battery_v : null);
  if (online) {
    if (d.battery_v != null && d.battery_v < 11.0) msgs.push("Batterij leeg - motoren uitgeschakeld door de OpenCR");
    if (l.bringup && l.robot_node === false) msgs.push("OpenCR antwoordt niet (batterij? USB?)");
    if (s.cpu_temp_c > 80) msgs.push("CPU " + Math.round(s.cpu_temp_c) + " °C");
    if (s.throttled & 1) msgs.push("Onderspanning van de Pi (voeding/batterij)");
    if (b[1] === "y") warns.push("batterij bijna leeg");
    if (s.updates && (s.updates.setup_behind || s.updates.image_behind)) warns.push("update beschikbaar");
    if (s.wifi_signal_dbm != null && s.wifi_signal_dbm < -72) warns.push("zwakke wifi");
    var chg = l.changes || [];
    if (chg.length) warns.push(chg.length + " aanpassing(en) t.o.v. de standaard");
    if (msgs.length) level = "bad"; else if (warns.length) level = "warn";
  }
  var temp = s.cpu_temp_c != null ? Math.round(s.cpu_temp_c) + " °C" : "--";
  var tc = s.cpu_temp_c > 80 ? "r" : s.cpu_temp_c > 70 ? "y" : "";
  var chips = ["bringup", "slam", "navigation", "joystick", "camera"].map(function (k) {
    var names = { bringup: "bringup", slam: "SLAM", navigation: "Navigatie", joystick: "joystick", camera: "camera" };
    return '<span class="chip' + (l[k] ? " on" : "") + '">' + names[k] + "</span>";
  }).join("");
  var nr = String(r.nr || "").padStart(2, "0");
  return '<div class="tile ' + level + (online ? "" : " off") + '">' +
    '<div class="name"><span>' + esc(r.robot) + '</span><span class="sub">' +
      (online ? '<span class="g">online</span>' : r.seen ? "offline sinds " + ago(age) : "nooit gezien") + "</span></div>" +
    (online ? '<div class="kv">' +
      "<span>Batterij</span><span class=\"" + b[1] + "\">" + b[0] + "</span>" +
      "<span>CPU</span><span>" + (s.cpu_load_1min != null ? s.cpu_load_1min : "--") + ' load, <span class="' + tc + '">' + temp + "</span></span>" +
      "<span>Wifi</span><span>" + (s.wifi_signal_dbm != null ? s.wifi_signal_dbm + " dBm" : "--") + "</span>" +
      "<span>Kaart</span><span>" + esc(l.map_running || "--") + "</span>" +
      "<span>USB / lidar</span><span>" + (l.usb_power === "off" ? "uit (spaarstand)" : l.usb_power === "on" ? "aan" : "--") +
        (s.lidar ? ", " + s.lidar.hours.toFixed(0) + " draaiuren" : "") + "</span>" +
      "<span>Software</span><span>" + esc(s.image_version || "--") + "</span>" +
      "<span>Uptime</span><span>" + (s.uptime_seconds ? ago(s.uptime_seconds) : "--") + "</span>" +
      "</div><div class=\"chips\">" + chips + "</div>" +
      msgs.map(function (m) { return '<div class="alert">' + esc(m) + "</div>"; }).join("") +
      (warns.length ? '<div class="warnmsg" title="' + esc((l.changes || []).map(function (c) { return "- " + c.text; }).join("\n")) + '">' +
        esc(warns.join(", ")) + "</div>" : "") + controls(r) : "") +
    '<div class="sub" style="margin-top:8px"><a href="http://10.0.' + Number(r.nr) + '.10:8080/" target="_blank">statuspagina</a> (wifi TB-AP-' + nr + ")</div></div>";
}
function refresh() {
  fetch("/api/robots").then(function (x) { return x.json(); }).then(function (j) {
    var rs = j.robots.slice().sort(function (a, b) { return Number(a.nr) - Number(b.nr); });
    lastRobots = rs; control = j.control;
    if (j.update_queue) renderUpdate(j.update_queue);
    document.getElementById("updAll").disabled = !control || !!(j.update_queue && (j.update_queue.running.length || j.update_queue.waiting.length));
    document.querySelectorAll("[data-all]").forEach(function (b) { b.disabled = !control; });
    var listening = rs.filter(function (r) { return r.listening; }).length;
    document.getElementById("allBar").style.display = j.view_only ? "none" : "";
    document.getElementById("ctrlInfo").textContent = !control ? "knoppen enkel op de laptop van de docent"
      : listening + " robot(s) luisteren naar opdrachten";
    if (document.activeElement && document.activeElement.tagName === "SELECT") return;   // don't close an open menu
    document.getElementById("grid").innerHTML = rs.map(function (r) { return tile(r, j.now); }).join("");
    var on = rs.filter(function (r) { return r.seen && j.now - r.seen < 20; }).length;
    document.getElementById("summary").textContent = on + " van " + rs.length + " robots online - " + new Date().toLocaleTimeString("nl-BE");
  }).catch(function () { document.getElementById("summary").textContent = "monitor niet bereikbaar"; });
}
function refreshLib() {
  fetch("/api/maps").then(function (x) { return x.json(); }).then(function (maps) {
    if (document.activeElement && document.activeElement.tagName === "SELECT") return;
    var robotOpts = lastRobots.filter(function (r) { return r.listening; }).map(function (r) {
      return '<option value="' + esc(r.robot) + '">' + esc(r.robot) + "</option>"; }).join("");
    var dis = control ? "" : " disabled";
    document.getElementById("libTable").innerHTML = maps.length ? maps.map(function (m) {
      return '<tr style="border-top:1px solid var(--border)"><td style="padding:4px 0"><b>' + esc(m.name) + '</b></td>' +
        '<td class="sub">van ' + esc(m.from || "?") + (m.created ? ", " + new Date(m.created * 1000).toLocaleString("nl-BE") : "") +
        " - " + m.zones + " zones, " + m.routes + " routes, " + m.size_kb + " kB</td>" +
        '<td style="text-align:right"><select data-libmap="' + esc(m.name) + '"' + dis + '><option value="">naar robot...</option>' +
        '<option value="*">alle robots die luisteren</option>' + robotOpts + "</select> " +
        '<button data-libdel="' + esc(m.name) + '"' + dis + ">Verwijder</button></td></tr>";
    }).join("") : '<tr><td class="sub">nog leeg</td></tr>';
  }).catch(function () {});
}
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-libdel]");
  if (b && confirm("Kaart '" + b.dataset.libdel + "' uit de bibliotheek verwijderen? (niet van de robots)"))
    fetch("/api/maps/delete", { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ name: b.dataset.libdel }) }).then(refreshLib);
});
document.getElementById("libFile").addEventListener("change", function (e) {
  var f = e.target.files[0];
  if (!f) return;
  f.arrayBuffer().then(function (buf) {
    return fetch("/api/maps/upload", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: buf });
  }).then(function (x) { return x.json(); }).then(function (j) {
    if (j.error) alert("Niet toegevoegd: " + j.error); refreshLib(); e.target.value = "";
  });
});
refresh(); setInterval(refresh, 2000);
refreshLib(); setInterval(refreshLib, 5000);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--remote-control", action="store_true",
                    help="knoppen ook toelaten vanaf andere toestellen dan deze laptop")
    ap.add_argument("--view-only", action="store_true", help="enkel kijken: geen knoppen")
    ap.add_argument("--control-port", type=int, default=0,
                    help="tweede poort waarop de knoppen altijd werken (Docker: enkel op 127.0.0.1 publiceren)")
    args = ap.parse_args()
    global REMOTE_CONTROL, VIEW_ONLY
    REMOTE_CONTROL = args.remote_control
    VIEW_ONLY = args.view_only
    threading.Thread(target=update_queue_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.daemon_threads = True   # waiting /poll requests don't block Ctrl+C
    if args.control_port:
        ctl = ThreadingHTTPServer(("0.0.0.0", args.control_port), Handler)
        ctl.daemon_threads = True
        ctl.control = True
        threading.Thread(target=ctl.serve_forever, daemon=True).start()
        print("Knoppen op poort %d" % args.control_port)
    print("TurtleBot Monitor op http://localhost:%d (robots sturen naar http://<dit-ip>:%d/push)"
          % (args.port, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
