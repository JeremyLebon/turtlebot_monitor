#!/usr/bin/env python3
"""TurtleBot fleet monitor - draait op de laptop van de docent.

Elke robot stuurt elke 5 s zijn status (system_info_node, MONITOR_URL) naar
deze server; de laptop zelf kan de robots niet bereiken (elke robot zit achter
de NAT van zijn eigen AP). Enkel de Python-standaardbibliotheek: werkt op
Windows, in WSL en op Linux.

    python monitor.py              # http://localhost:8090
    python monitor.py --port 9000

De robots moeten deze laptop kunnen bereiken: zet het adres in
turtlebot_setup/monitor.conf (MONITOR_URL=http://<ip-van-de-laptop>:8090/push)
en geef de laptop een vast IP op het switch-subnet. Windows: sta Python toe
in de firewall (privénetwerk) wanneer daarom gevraagd wordt.
"""
import argparse
import csv
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_CSV = os.path.join(HERE, "..", "turtlebot_config.csv")

robots = {}          # hostname -> {"data": payload, "seen": time, "addr": ip}
lock = threading.Lock()


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

    def do_POST(self):
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
            return self._send(200, json.dumps({"now": time.time(), "robots": out}), "application/json")
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
</style></head><body>
<header><h1>TurtleBot Monitor</h1><span class="sub" id="summary">laden...</span></header>
<div class="grid" id="grid"></div>
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
      (warns.length ? '<div class="warnmsg">' + esc(warns.join(", ")) + "</div>" : "") : "") +
    '<div class="sub" style="margin-top:8px"><a href="http://10.0.' + Number(r.nr) + '.10:8080/" target="_blank">statuspagina</a> (wifi TB-AP-' + nr + ")</div></div>";
}
function refresh() {
  fetch("/api/robots").then(function (x) { return x.json(); }).then(function (j) {
    var rs = j.robots.slice().sort(function (a, b) { return Number(a.nr) - Number(b.nr); });
    document.getElementById("grid").innerHTML = rs.map(function (r) { return tile(r, j.now); }).join("");
    var on = rs.filter(function (r) { return r.seen && j.now - r.seen < 20; }).length;
    document.getElementById("summary").textContent = on + " van " + rs.length + " robots online - " + new Date().toLocaleTimeString("nl-BE");
  }).catch(function () { document.getElementById("summary").textContent = "monitor niet bereikbaar"; });
}
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("TurtleBot Monitor op http://localhost:%d (robots sturen naar http://<dit-ip>:%d/push)"
          % (args.port, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
