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
een robot-AP de monitor opent, kan niets bedienen. Vanaf een ander toestel:
"Docent aanmelden" met de docenten-PIN (dezelfde als Beheer op de robots).

    python monitor.py --set-pin    # docenten-PIN van de monitor (eerste keer)
    docker exec -it turtlebot-monitor python monitor.py --set-pin   # in Docker

De robots moeten deze laptop (of de RPi, README optie E) kunnen bereiken: zet
het adres in turtlebot_robot/monitor.conf
(MONITOR_URL=http://<ip-van-de-monitor>:8090/push) en geef de monitor een vast
IP op het switch-subnet. Windows: sta Python toe
in de firewall (privénetwerk) wanneer daarom gevraagd wordt.
"""
import argparse
import base64
import hashlib
import csv
import glob
import io
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_CSV = os.path.join(HERE, "..", "turtlebot_config.csv")
# Kaartenbibliotheek: kaartbundels (kaart + zones + routes) van de robots,
# om op andere robots te zetten. Bestanden: monitor/maps/<naam>.tbmap.json
MAP_LIB = os.path.join(HERE, "maps")
PIN_FILE = os.path.join(HERE, "docent_pin")   # hash of the docenten-PIN (not in git)
SESSION_S = 12 * 3600                         # "Docent aanmelden" lasts one school day
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
ROBOT_COMMANDS = {"stop_all", "beep", "update", "reboot", "shutdown", "map_upload", "map_download", "set_pin",
                  "wifitest"} | {
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


# Wifitest (robot image 1.6.0+): robots send data to /wifitest/upload for N s,
# all together ("samen": do they slow each other down?) or one after the
# other ("na elkaar": what does each get alone?). The monitor counts what
# arrives; robots on the RPi's wire, so only the robot -> AP hop is measured.
WIFITEST_LOG = os.path.join(HERE, "wifitest_log.jsonl")
wt = {"test": None, "history": []}     # test: {"id", "mode", "secs", "started", "order", "robots": {...}}


def wifitest_start(names, mode, secs):
    with cond:
        cur = wt["test"]
        if cur and not cur.get("done"):
            return "er loopt al een wifitest"
        cmd_seq[0] += 1
        wt["test"] = {"id": cmd_seq[0], "mode": mode, "secs": secs, "started": time.time(), "order": list(names),
                      "robots": {n: {"bytes": 0, "t0": None, "t1": None} for n in names}}
    threading.Thread(target=_wifitest_run, args=(wt["test"],), daemon=True).start()
    return None


def _wifitest_run(t):
    groups = [t["order"]] if t["mode"] == "samen" else [[n] for n in t["order"]]
    for g in groups:
        for n in g:
            t["robots"][n]["sent"] = time.time()
        queue_command(g, "wifitest", "%d:%d" % (t["id"], t["secs"]))
        end = time.time() + t["secs"] + 20      # poll delay + a robot that never starts
        while time.time() < end and not all(t["robots"][n]["t1"] and time.time() - t["robots"][n]["t1"] > 1
                                            for n in g):
            time.sleep(0.5)
    with cond:
        t["done"] = time.time()
        res = wifitest_result(t)
        wt["history"] = ([res] + wt["history"])[:20]
    try:
        with open(WIFITEST_LOG, "a") as f:
            f.write(json.dumps(res) + "\n")
    except OSError:
        pass


def wifitest_result(t):
    out = {}
    for n, r in t["robots"].items():
        dt = (r["t1"] or 0) - (r["t0"] or 0)
        out[n] = round(r["bytes"] * 8 / dt / 1e6, 1) if dt > 0.5 else None
    return {"id": t["id"], "mode": t["mode"], "secs": t["secs"], "t": t["started"], "mbps": out,
            "total": round(sum(v for v in out.values() if v), 1), "done": bool(t.get("done"))}


def wifitest_history():
    """Last tests, also from earlier monitor runs (wifitest_log.jsonl)."""
    if not wt["history"]:
        try:
            with open(WIFITEST_LOG) as f:
                wt["history"] = [json.loads(l) for l in f.read().splitlines()[-20:]][::-1]
        except (OSError, ValueError):
            pass
    return wt["history"]


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


def pin_hash(pin):
    """Same format as fenix_admin.py on the robot (Beheer-PIN); only this hash
    is sent to the robots, never the PIN."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200000).hex()
    return "pbkdf2_sha256:200000:%s:%s" % (salt, h)


def pin_ok(pin, stored):
    try:
        algo, it, salt, h = stored.split(":")
        got = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(it)).hex()
    except ValueError:
        return False
    return algo == "pbkdf2_sha256" and secrets.compare_digest(got, h)


def load_pin():
    try:
        with open(PIN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def save_pin(h):
    with open(PIN_FILE + ".tmp", "w") as f:
        f.write(h + "\n")
    os.chmod(PIN_FILE + ".tmp", 0o600)
    os.replace(PIN_FILE + ".tmp", PIN_FILE)


sessions = {}        # cookie token -> expiry ("Docent aanmelden")
login_fails = {}     # ip -> [wrong PINs in a row, locked until]


WIFI_PLAN_CSV = os.path.join(HERE, "wifi_kanalen.csv")
WIFI_SCAN = os.path.join(HERE, "wifi_scan.txt")
# Monitor on its own RPi (install_rpi.sh): the container asks the host to shut
# down or reboot through a file in this folder; a systemd path unit on the RPi
# acts on it. POWER_MARK exists only there, so the buttons show only there.
POWER_MARK = os.path.join(HERE, ".rpi_power")
POWER_REQUEST = os.path.join(HERE, "power_request")


def wifi_plan():
    """Planned AP channel per robot (docs/ap-migration.md; adapt to the table
    layout in the room): {"9": {"5": 36, "2.4": 11, "width": 20}}."""
    try:
        with open(WIFI_PLAN_CSV, newline="") as f:
            return {r["nr"].strip(): {"5": int(r["kanaal_5ghz"]), "2.4": int(r["kanaal_24ghz"]),
                                      "width": int(r.get("breedte_mhz") or 20)}
                    for r in csv.DictReader(f)}
    except (OSError, KeyError, ValueError):
        return {}


def _center_mhz(ch, freq, width):
    """Centre of an AP's channel block from its primary channel (the scan
    gives only the primary): 5 GHz blocks are aligned (36-48 = one 80 MHz
    block); 2.4 GHz 40 MHz is a guess (low channels bond upwards)."""
    if width <= 20:
        return freq
    if freq > 5000:
        base = 149 if ch >= 149 else 36
        start = base + (ch - base) // (width // 5) * (width // 5)
        return 5000 + 5 * start + width // 2 - 10
    return freq + 10 if ch <= 7 else freq - 10


def wifi_scan():
    """Networks around the monitor RPi: its free wlan0 scans every minute
    (install_rpi.sh, systemd timer -> wifi_scan.txt). One entry per radio:
    the school APs send 4-5 SSIDs (eduroam, VIVES-GUEST, ...) per radio."""
    try:
        age = time.time() - os.path.getmtime(WIFI_SCAN)
        if age > 300:
            return None
        with open(WIFI_SCAN) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    radios = {}
    for line in lines:
        p = [x.replace("\\:", ":") for x in re.split(r"(?<!\\):", line)]
        try:
            ssid, bssid, ch, freq = p[0], p[1], int(p[2]), int(p[3].split()[0])
            width, signal = int(p[4].split()[0]), int(p[5])
        except (IndexError, ValueError):
            continue
        # same radio = same BSSID except the last digit; our own APs never merge
        # (TB-AP-04 and -07 differ only in that digit)
        key = (ssid if ssid.startswith("TB-AP-") else bssid[:-1], ch)
        r = radios.setdefault(key, {"ssids": [], "ch": ch, "freq": freq, "width": width,
                                                 "center": _center_mhz(ch, freq, width), "signal": signal})
        if ssid and ssid not in r["ssids"]:
            r["ssids"].append(ssid)
        r["signal"] = max(r["signal"], signal)
    return {"age": round(age), "radios": sorted(radios.values(), key=lambda r: (r["freq"], -r["signal"]))}


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

    def _send(self, code, body, ctype, cookie=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cookie is not None:
            self.send_header("Set-Cookie", "tbmon=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
                             % (cookie, SESSION_S if cookie else 0))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        m = re.search(r"(?:^|;\s*)tbmon=([\w-]+)", self.headers.get("Cookie", ""))
        return m.group(1) if m and sessions.get(m.group(1), 0) > time.time() else None

    def _may_control(self):
        # control server (--control-port, Docker: published on 127.0.0.1 only),
        # a browser on this laptop itself, or a teacher logged in with the PIN
        return not VIEW_ONLY and (getattr(self.server, "control", False) or REMOTE_CONTROL
                                  or self.client_address[0] in ("127.0.0.1", "::1")
                                  or self._session() is not None)

    def _login(self):
        ip, now = self.client_address[0], time.time()
        fail = login_fails.setdefault(ip, [0, 0])
        if fail[1] > now:
            return self._send(429, json.dumps({"error": "Te veel foute PIN's - wacht %d s" % (fail[1] - now)}),
                              "application/json")
        stored = load_pin()
        if not stored:
            return self._send(409, json.dumps({"error": "Nog geen docenten-PIN op de monitor. Stel er een in met "
                                                        "'Docenten-PIN' op de monitor zelf, of met --set-pin."}),
                              "application/json")
        try:
            pin = str(self._read_json()["pin"])
        except (ValueError, KeyError, TypeError):
            return self._send(400, "bad request", "text/plain")
        if not pin_ok(pin, stored):
            fail[0] += 1
            if fail[0] >= 5:            # 5 wrong in a row: 1 minute pause (PIN = few digits)
                fail[:] = [0, now + 60]
            return self._send(403, json.dumps({"error": "Foute PIN"}), "application/json")
        login_fails.pop(ip, None)
        for t in [t for t, exp in sessions.items() if exp < now]:
            del sessions[t]
        token = secrets.token_urlsafe(24)
        sessions[token] = now + SESSION_S
        return self._send(200, json.dumps({"ok": True}), "application/json", cookie=token)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(min(n, 20 * 1024 * 1024)))   # map bundles: some 100 kB

    def do_POST(self):
        if self.path == "/api/login":
            if VIEW_ONLY:
                return self._send(403, json.dumps({"error": "monitor gestart met --view-only"}), "application/json")
            return self._login()
        if self.path == "/api/logout":
            sessions.pop(self._session(), None)
            return self._send(200, json.dumps({"ok": True}), "application/json", cookie="")
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
        if self.path == "/api/rpi_power":   # shut down / reboot the monitor RPi itself
            if not self._may_control():
                return self._send(403, json.dumps({"error": "knoppen enkel na Docent aanmelden"}), "application/json")
            try:
                action = self._read_json()["action"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            if action not in ("poweroff", "reboot") or not os.path.exists(POWER_MARK):
                return self._send(400, json.dumps({"error": "kan niet op dit toestel"}), "application/json")
            with open(POWER_REQUEST, "w") as f:
                f.write(action + "\n")
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
        if self.path == "/api/set_pin":   # docenten-PIN (Beheer) for every listening robot
            if not self._may_control():
                return self._send(403, json.dumps({"error": "knoppen enkel vanaf de laptop zelf (localhost)"}),
                                  "application/json")
            try:
                r = self._read_json()
                pin, names = str(r["pin"]), [str(n)[:40] for n in r["robots"]]
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            if not (pin.isdigit() and 4 <= len(pin) <= 8):
                return self._send(400, json.dumps({"error": "PIN moet 4 tot 8 cijfers zijn"}), "application/json")
            h = pin_hash(pin)
            save_pin(h)                 # same PIN for "Docent aanmelden" on the monitor
            queue_command(names, "set_pin", h)
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
        if self.path.startswith("/wifitest/upload?"):   # robot: wifitest data, counted and dropped
            return self._wifitest_upload()
        if self.path == "/api/wifitest":
            if VIEW_ONLY or not self._may_control():
                return self._send(403, json.dumps({"error": "eerst Docent aanmelden"}), "application/json")
            try:
                r = self._read_json()
                names, mode, secs = [str(n)[:40] for n in r["robots"]], r.get("mode", "samen"), int(r.get("secs", 15))
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            if not names or mode not in ("samen", "na_elkaar"):
                return self._send(400, json.dumps({"error": "geen robots of onbekende modus"}), "application/json")
            err = wifitest_start(names, "samen" if mode == "samen" else "na elkaar", max(5, min(60, secs)))
            return self._send(409 if err else 200, json.dumps({"error": err} if err else {"ok": True}),
                              "application/json")
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

    def _wifitest_upload(self):
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        name, tid = q.get("robot", [""])[0][:40], q.get("id", [""])[0]
        t = wt["test"]
        if not t or str(t["id"]) != tid or name not in t["robots"]:
            return self._send(409, "geen wifitest bezig", "text/plain")
        r, stop = t["robots"][name], time.time() + t["secs"] + 5
        self.close_connection = True
        try:
            self.connection.settimeout(5)
            while time.time() < stop:
                b = self.rfile.read1(262144)
                if not b:
                    break
                now = time.time()
                if r["t0"] is None:
                    r["t0"] = now
                r["bytes"] += len(b)
                r["t1"] = now
        except OSError:
            pass   # robot closes the connection when its time is up

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
                                               "session": self._session() is not None,
                                               "pin_set": bool(load_pin()),
                                               "view_only": VIEW_ONLY,
                                               "wifi_plan": wifi_plan(),
                                               "wifi_scan": wifi_scan(),
                                               "wifitest": {"current": wifitest_result(wt["test"]) if wt["test"] else None,
                                                            "busy": bool(wt["test"] and not wt["test"].get("done")),
                                                            "history": wifitest_history()},
                                               "rpi_power": os.path.exists(POWER_MARK)}),
                              "application/json")
        if self.path.split("?")[0] in ("/", "/index.html"):   # also "/?kiosk" (kiosk.sh)
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
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; align-items:start; }
.tile { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:12px; border-left:5px solid var(--muted); }
.tile.ok { border-left-color:var(--green); } .tile.warn { border-left-color:var(--yellow); } .tile.bad { border-left-color:var(--red); }
.tile.off { opacity:.55; }
.name { font-weight:700; font-size:1rem; display:flex; justify-content:space-between; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; margin-top:8px; font-size:.85rem; }
.kv > span:nth-child(odd) { color:var(--muted); }
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
.tile { min-width:0; }
.head { display:flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }
.head b { font-size:1rem; }
.head .sum { margin-left:auto; font-size:.82rem; text-align:right; display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.hi { display:inline-flex; align-items:center; gap:3px; color:var(--muted); }
.hi.g { color:var(--green); } .hi.y { color:var(--yellow); } .hi.r { color:var(--red); }
button svg, .chip svg { vertical-align:-3px; margin-right:4px; }
.chip svg { margin-right:3px; }
.head input { margin:0; cursor:pointer; }
.caret { color:var(--muted); width:1em; }
.warnline { margin-top:6px; font-size:.8rem; color:var(--yellow); }
.info { margin-top:8px; font-size:.8rem; color:var(--muted); }
.btns select { flex:1 1 10em; min-width:0; max-width:100%; }
.btns button.on { background:var(--accent); border-color:var(--accent); color:#0b1a2b; font-weight:600; }
.btns button.on:disabled { opacity:.8; }   /* without login: still shows what runs */
.btns button.ico svg { margin-right:0; }
details.bar { display:block; }
details.bar summary { cursor:pointer; }
.spec { width:100%; max-width:760px; display:block; margin-top:8px; }
.spec text { fill:var(--muted); font-size:11px; }
.chtab { border-collapse:collapse; margin-top:8px; font-size:.82rem; }
.chtab td, .chtab th { padding:2px 10px 2px 0; text-align:left; }
.chtab th { color:var(--muted); font-weight:400; }
</style></head><body>
<header><h1>TurtleBot Monitor</h1><span class="sub" id="summary">laden...</span>
  <span style="margin-left:auto;display:flex;gap:6px"><button id="openAll"></button>
  <button id="loginBtn" style="display:none">Docent aanmelden</button></span></header>
<div class="bar" id="allBar">
  <b>Alle robots:</b>
  <button class="stop" data-all="stop_all" data-icon="stop" title="Alle launches stoppen - motoren stil">STOP ALLES</button>
  <button data-all="start:bringup" data-icon="play">Bringup starten</button>
  <button id="updAll" title="Software bijwerken (git + Docker-image) voor de aangevinkte robots, in groepjes zodat internet en lessen niet vastlopen">Bijwerken</button>
  <select id="selPick" title="Robots aanvinken om bij te werken"><option value="">aanvinken...</option>
    <option value="all">alle die luisteren</option><option value="upd">met update beschikbaar</option><option value="none">geen</option></select>
  <select id="updPar" title="Hoeveel robots tegelijk"><option value="1">1 tegelijk</option><option value="2" selected>2 tegelijk</option><option value="3">3 tegelijk</option></select>
  <button id="updCancel" style="display:none" title="Wachtende robots niet meer bijwerken (lopende updates werken af)">Wachtrij stoppen</button>
  <button data-all="shutdown" data-icon="power" title="Alle robots uitschakelen (einde les)">Uitschakelen</button>
  <button id="pinAll" data-icon="beheer" title="Docenten-PIN voor Beheer op de statuspagina, voor alle robots die luisteren, en voor Docent aanmelden op deze monitor (enkel de hash gaat naar de robots)">Docenten-PIN</button>
  <span class="sub" id="ctrlInfo"></span>
  <span id="rpiPower" style="display:none;margin-left:auto">Monitor-RPi:
    <button data-power="reboot" data-icon="reboot">Herstarten</button> <button data-power="poweroff" data-icon="power">Afsluiten</button></span>
</div>
<div class="bar" id="updBox" style="display:none"></div>
<div class="grid" id="grid"></div>
<details class="bar" id="chanBox" style="display:none;margin-top:14px"><summary id="chanSum"></summary><div id="chanBody"></div></details>
<details class="bar" id="wtBox" style="display:none;margin-top:14px"><summary id="wtSum"></summary>
  <div id="wtCtl" class="sub" style="margin-top:8px">
    <select id="wtMode"><option value="samen">alle robots samen</option><option value="na_elkaar">robots na elkaar</option></select>
    <select id="wtSecs"><option>10</option><option selected>15</option><option>30</option></select> s
    <button id="wtStart" data-icon="wifi">Start wifitest</button>
    <span>aangevinkte robots, of alle robots als er niets aangevinkt is. Niet tijdens het rijden: de test vult de wifi.</span>
  </div>
  <div id="wtBody"></div></details>
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
// Kiosk screen on the monitor RPi (kiosk.sh opens ...?kiosk): no links, the RPi cannot
// reach the robots behind their AP and a full-screen browser has no way back.
var KIOSK = /[?&]kiosk\b/.test(location.search);
// Icons from the Fenix status page on the robots (common.js FENIX_ICONS), so
// the monitor looks like the robot pages; bell is new.
var FX = {
  stop: '<path d="M8 2h8l6 6v8l-6 6H8l-6-6V8z"/><path d="M9 9h6v6H9z"/>',
  play: '<path d="M7 4l13 8-13 8z" fill="currentColor"/>',
  update: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  power: '<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>',
  reboot: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  beheer: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlocked: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
  fit: '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/>',
  maps: '<path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z"/><path d="M9 4v14M15 6v14"/>',
  nav: '<path d="M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15M15 6v15"/>',
  gamepad: '<path d="M6 11h4M8 9v4M15 12h.01M18 10h.01"/><path d="M17.3 5H6.7a4 4 0 0 0-4 3.6L2 15a3 3 0 0 0 5.5 1.7L9 15h6l1.5 1.7A3 3 0 0 0 22 15l-.7-6.4A4 4 0 0 0 17.3 5z"/>',
  camera: '<path d="M3 7h4l2-3h6l2 3h4v13H3z"/><circle cx="12" cy="13" r="4"/>',
  robot: '<rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 7V4M9 12h.01M15 12h.01M9 16h6"/>',
  wifi: '<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1" fill="currentColor"/>',
  lidar: '<circle cx="12" cy="12" r="2"/><path d="M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14M8 8a5 5 0 0 0 0 8M16 8a5 5 0 0 1 0 8"/>',
};
function fxIcon(name, size) {
  return '<svg width="' + (size || 16) + '" height="' + (size || 16) + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + FX[name] + "</svg>";
}
document.querySelectorAll("[data-icon]").forEach(function (el) { el.innerHTML = fxIcon(el.dataset.icon) + el.innerHTML; });
// Header icons of a tile, drawn like the Fenix header: battery, wifi bars, Pi temperature
function battIcon(pct) {
  var w = pct == null ? 0 : Math.max(1, Math.round(17 * pct / 100));
  return '<svg width="24" height="13" viewBox="0 0 26 14" aria-hidden="true"><rect x="1" y="1" width="21" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<rect x="22.5" y="4.5" width="2.5" height="5" rx="1" fill="currentColor"/><rect x="3" y="3" width="' + w + '" height="8" rx="1" fill="currentColor"/></svg>';
}
function wifiIcon(dbm) {
  var bars = dbm == null ? 0 : dbm > -55 ? 4 : dbm > -65 ? 3 : dbm > -72 ? 2 : dbm > -80 ? 1 : 0;
  return '<svg width="17" height="13" viewBox="0 0 18 14" aria-hidden="true">' + [0, 1, 2, 3].map(function (i) {
    return '<rect x="' + (i * 4.6) + '" y="' + (10 - i * 3) + '" width="3.4" height="' + (4 + i * 3) + '" rx="1" fill="currentColor" opacity="' + (i < bars ? 1 : 0.25) + '"/>';
  }).join("") + "</svg>";
}
function tempIcon() {
  return '<svg width="11" height="16" viewBox="0 0 12 18" aria-hidden="true"><path d="M4 2.5a2 2 0 0 1 4 0v8.2a3.6 3.6 0 1 1-4 0z" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<circle cx="6" cy="13.6" r="2" fill="currentColor"/><rect x="5.2" y="8" width="1.6" height="5" fill="currentColor"/></svg>';
}
function esc(t) { return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
function ago(s) { return s < 60 ? Math.round(s) + " s" : s < 3600 ? Math.round(s / 60) + " min" : Math.round(s / 3600) + " u"; }
function batt(v) {   // 0 % = 11.0 V: there the OpenCR switches the motors off
  if (v == null) return ["--", ""];
  var p = Math.max(0, Math.min(100, (v - 11.0) / 1.3 * 100));
  return [v.toFixed(1) + " V (" + Math.round(p) + " %)", v < 11.2 ? "r" : v < 11.4 ? "y" : "g", Math.round(p) + " %", p];
}
var CMD_TEXT = { wifitest: "wifitest", stop_all: "stop alles", beep: "piep", update: "bijwerken", reboot: "herstarten", shutdown: "uitschakelen",
                 map_upload: "kaart naar bibliotheek", map_download: "kaart uit bibliotheek", set_pin: "PIN instellen" };
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
// Kiosk screen on the RPi: whoever stands in front of it after the teacher must
// not keep the buttons - log out after KIOSK_IDLE_S without mouse/touch/keys.
var KIOSK_IDLE_S = 600, lastActivity = Date.now();
["pointerdown", "keydown", "wheel"].forEach(function (ev) {
  document.addEventListener(ev, function () { lastActivity = Date.now(); }, true);
});
setInterval(function () {
  if (KIOSK && lastJ && lastJ.session && Date.now() - lastActivity > KIOSK_IDLE_S * 1000) post("/api/logout");
}, 10000);
document.getElementById("loginBtn").addEventListener("click", function () {
  if (this.dataset.session) { post("/api/logout"); return; }
  var pin = prompt("Docenten-PIN:");
  if (pin) post("/api/login", { pin: pin });
});
function selectedNames() {
  return lastRobots.filter(function (r) { return r.listening && selected[r.robot]; }).map(function (r) { return r.robot; });
}
document.getElementById("updAll").addEventListener("click", function () {
  var names = selectedNames();
  if (!names.length) { alert("Vink eerst de robots aan die bijgewerkt moeten worden."); return; }
  var par = document.getElementById("updPar").value;
  if (!confirm("Software bijwerken voor " + names.length + " robots, " + par + " tegelijk?\n" +
               "Bij een nieuwe image herstart de container: lopende launches stoppen.")) return;
  post("/api/update_all", { robots: names, parallel: Number(par) });
});
document.getElementById("pinAll").addEventListener("click", function () {
  var names = lastRobots.filter(function (r) { return r.listening; }).map(function (r) { return r.robot; });
  if (!names.length) { alert("Geen enkele robot luistert naar de monitor."); return; }
  var pin = prompt("Nieuwe docenten-PIN voor Beheer (4-8 cijfers), voor " + names.length + " robots:");
  if (pin === null) return;
  if (!/^[0-9]{4,8}$/.test(pin)) { alert("PIN moet 4 tot 8 cijfers zijn."); return; }
  if (prompt("Nog eens:") !== pin) { alert("De PIN's verschillen - niets veranderd."); return; }
  post("/api/set_pin", { pin: pin, robots: names });
});
document.getElementById("updCancel").addEventListener("click", function () { post("/api/update_all/cancel"); });
document.getElementById("selPick").addEventListener("change", function (e) {
  var v = e.target.value;
  e.target.value = "";
  selected = {};
  lastRobots.forEach(function (r) {
    if (r.listening && (v === "all" || (v === "upd" && updateAvailable(((r.data || {}).status || {}).updates))))
      selected[r.robot] = true;
  });
  render();
});
document.querySelectorAll("[data-power]").forEach(function (b) {
  b.addEventListener("click", function () {
    var off = b.dataset.power === "poweroff";
    if (!confirm(off ? "De monitor-RPi afsluiten?\nDe robots blijven werken; de monitor is weg tot je de RPi opnieuw aanzet "
                     + "(stekker uit en in, of de aan/uit-knop van de RPi)."
                     : "De monitor-RPi herstarten? (ongeveer 1 minuut weg)")) return;
    post("/api/rpi_power", { action: b.dataset.power });
  });
});
document.getElementById("openAll").addEventListener("click", function () {
  var anyClosed = lastRobots.some(function (r) { return !openTiles[r.robot]; });
  lastRobots.forEach(function (r) { openTiles[r.robot] = anyClosed; });
  saveOpen(); render();
});
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
  var h = e.target.closest("[data-toggle]");
  if (h && !e.target.matches("input")) {
    openTiles[h.dataset.toggle] = !openTiles[h.dataset.toggle];
    saveOpen(); render();
  }
});
document.addEventListener("change", function (e) {
  if (e.target.matches("input[data-sel]")) { selected[e.target.dataset.sel] = e.target.checked; render(); }
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
// Tile buttons: launches as icon toggles (filled = running, click = stop),
// then the robot actions; only the map list (variable) stays a dropdown.
var LAUNCH_BTNS = [["bringup", "robot", "Bringup"], ["slam", "lidar", "SLAM"], ["navigation", "nav", "Navigatie"],
                   ["joystick", "gamepad", "Joystick"], ["camera", "camera", "Camera"]];
function controls(r) {
  var dis = control && r.listening ? "" : " disabled", rb = ' data-robot="' + esc(r.robot) + '"' + dis;
  var l = (r.data || {}).launch || {};
  function btn(cmd, icon, label, title, cls) {
    return '<button data-cmd="' + cmd + '"' + rb + (cls ? ' class="' + cls + '"' : "") + ' title="' + esc(title) + '">' +
      fxIcon(icon, 14) + label + "</button>";
  }
  var launches = LAUNCH_BTNS.map(function (b) {
    return btn((l[b[0]] ? "stop:" : "start:") + b[0], b[1], b[2], (l[b[0]] ? "Stop " : "Start ") + b[0], l[b[0]] ? "on" : "");
  }).join("");
  var maps = (l.maps || []).length ? '<select data-robot="' + esc(r.robot) + '"' + dis + ' title="Kaart van deze robot naar de kaartenbibliotheek">' +
    '<option value="">kaart naar bibliotheek...</option>' + l.maps.map(function (m) {
      return '<option value="map_upload|' + esc(m) + '">' + esc(m) + "</option>"; }).join("") + "</select>" : "";
  var res = r.result ? '<div class="res">' + esc(cmdText(r.result.cmd)) + ": " +
    (r.result.ok === null ? "verstuurd..." : (r.result.ok ? '<span class="g">OK</span> ' : '<span class="r">mislukt</span> ') + esc(r.result.message)) + "</div>" : "";
  return '<div class="btns">' + launches + "</div>" +
    '<div class="btns">' + btn("stop_all", "stop", "Stop", "Alle launches stoppen - motoren stil", "stop") +
    btn("beep", "bell", "", "Piep: laat de robot piepen, om hem terug te vinden", "ico") +
    btn("update", "update", "Bijwerken", "Software bijwerken (nieuw image + setup)") +
    btn("reboot", "reboot", "", "Robot herstarten", "ico") + btn("shutdown", "power", "", "Robot uitschakelen", "ico") + "</div>" +
    (maps ? '<div class="btns">' + maps + "</div>" : "") + res;
}
// Robot state from the launches (the active control source itself isn't in
// the monitor payload): what the class is doing at a glance
function robotState(l) {
  if (!l.bringup) return ["Inactief", ""];
  if (l.navigation) return ["Navigatie", "g"];
  if (l.slam) return ["Kaart maken", "g"];
  return ["Bringup", "g"];
}
var SVC_NAME = { zenoh: "Zenoh", rosbridge: "rosbridge", webpagina: "webpagina", launch_control: "launch control",
                 twist_mux: "twist_mux", cmd_vel_guard: "cmd_vel_guard", nav_web: "nav-web", system_info: "systeeminfo" };
// Since 0.20/0.23 the robots send which services stopped or crashed and the
// last ERROR/FATAL lines from /rosout (system_info_node)
function health(s, msgs, warns, tips) {
  var sv = s.services || {};
  Object.keys(sv).forEach(function (k) {
    // terminal: deliberately off without a password (older images report false)
    if (sv[k] === false && k !== "terminal") msgs.push("Dienst gestopt: " + (SVC_NAME[k] || k));
  });
  var sup = s.supervisor || {}, t = s.time_unix || 0;
  Object.keys(sup).forEach(function (k) {
    if (sup[k].last > t - 600) warns.push((SVC_NAME[k] || k) + " gecrasht + herstart");
  });
  var errs = (s.rosout_errors || []).filter(function (e) { return e.t > t - 300; });
  if (errs.length) {
    warns.push(errs.length + " ROS-fout(en) laatste 5 min");
    errs.forEach(function (e) { tips.push(e.level + " " + e.node + ": " + e.msg); });
  }
}
// Linux load average (1 min) of the whole Pi, as a share of its cores: load 4
// on the 4 cores of a Pi 5 = all busy; above that processes wait (Nav2 slows
// down). Per-core usage (%) in the tooltip.
// CPU use = average of the cores over the last ~2 s (system_info_node). The
// load average is shown small: it counts processes waiting for the CPU *or*
// for the SD card, so it jumps between 0,1 and 2 with the same launches.
function cpuLoad(s) {
  var cores = s.cpu_percent_per_core || [], n = s.cpu_count || cores.length || 4;
  if (!cores.length && s.cpu_load_1min == null) return "--";
  var use = cores.length ? Math.round(cores.reduce(function (a, b) { return a + b; }, 0) / cores.length) : null;
  var c = use == null ? "" : use > 90 ? "r" : use > 70 ? "y" : "";
  var tip = (use != null ? "CPU-gebruik " + use + " % (gemiddelde van " + n + " kernen, laatste 2 s)\n" +
    cores.map(function (v, i) { return "kern " + i + ": " + Math.round(v) + " %"; }).join("\n") + "\n" : "") +
    (s.cpu_load_1min != null ? "Load " + s.cpu_load_1min + " (1 min)" + (s.cpu_load_5min != null ? ", " + s.cpu_load_5min + " (5 min)" : "") +
      ": processen die wachten op de CPU of op de SD-kaart; " + n + " = alle kernen vol. Schommelt sterk." : "");
  return '<span class="' + c + '" title="' + esc(tip) + '">' + (use != null ? use + " % gebruikt" : "") +
    (s.cpu_load_1min != null ? ' <span class="sub">(load ' + String(s.cpu_load_1min).replace(".", ",") + ")</span>" : "") + "</span>";
}
// Traffic on the robot's wifi (wlan0), in kbit/s from system_info_node: camera
// streams, Lichtblick and rosbridge add up; a busy link makes the pages slow.
function rate(k) { return k < 1000 ? Math.round(k) + " kbit/s" : (k / 1000).toFixed(1).replace(".", ",") + " Mbit/s"; }
// Average over the last net_avg_s seconds when the robot sends it (image 1.5.0+), else the 2 s value
function netRx(s) { return s.net_rx_kbps_avg != null ? s.net_rx_kbps_avg : s.net_rx_kbps; }
function netTx(s) { return s.net_tx_kbps_avg != null ? s.net_tx_kbps_avg : s.net_tx_kbps; }
// Who uses the robot: open Fenix pages (rosbridge websockets, image 1.7.0+)
// on how many devices, and laptops on Zenoh (ROS 2 / Lichtblick)
function usersText(s) {
  var w = s.web_clients, z = (s.zenoh_clients || []).length, parts = [];
  if (w) parts.push(w.pages + " pagina" + (w.pages === 1 ? "" : "'s") + " op " + w.devices + " toestel" + (w.devices === 1 ? "" : "len"));
  if (w && w.terminal) parts.push(w.terminal + " terminal");
  if (s.zenoh_clients) parts.push(z + " ROS-laptop" + (z === 1 ? "" : "s"));
  return parts.length ? '<span title="pagina = open Fenix-pagina (rosbridge-verbinding); toestel = verschillend IP-adres; ' +
    'ROS-laptop = Zenoh-client (ros2, Lichtblick)' + (z ? ": " + esc(s.zenoh_clients.join(", ")) : "") + '">' + parts.join(" · ") + "</span>" : "<span>--</span>";
}
function wifiLoad(s) {
  var rx = netRx(s), tx = netTx(s);
  if (rx == null || tx == null) return "<span>--</span>";
  var tot = rx + tx, c = tot > 30000 ? "r" : tot > 15000 ? "y" : "";
  return '<span class="' + c + '" title="naar de robot / van de robot (wlan0)' +
    (s.net_rx_kbps_avg != null ? ", gemiddelde over " + s.net_avg_s + " s" : ", momentopname") + '">↓ ' + rate(rx) + " · ↑ " + rate(tx) + "</span>";
}
// The robot itself compares its image (Docker Hub) and setup repo (GitHub) with
// the newest ones, when it has internet; null = not checked (yet).
function updateAvailable(u) { return !!(u && (u.image_behind || u.setup_behind)); }
function softwareState(u) {
  if (updateAvailable(u))
    return '<br><span class="y">update beschikbaar (' + [u.image_behind && "image", u.setup_behind && "setup"]
      .filter(Boolean).join(" + ") + ")</span>";
  if (u && u.image_behind === false && u.setup_behind !== true) return ' <span class="g">nieuwste</span>';
  return u && u.error ? '<br><span class="sub" title="' + esc(u.error) + '">niet nagekeken</span>' : "";
}
// Per robot: details open (remembered in this browser) / ticked for "Bijwerken"
var openTiles = {}, selected = {}, lastJ = null;
try { openTiles = JSON.parse(localStorage.getItem("tbmonOpen") || "{}"); } catch (e) {}
function saveOpen() { try { localStorage.setItem("tbmonOpen", JSON.stringify(openTiles)); } catch (e) {} }
function tile(r, now, common) {
  var d = r.data || {}, s = d.status || {}, l = d.launch || {};
  var age = r.seen ? now - r.seen : null, online = age != null && age < 20;
  var level = "ok", msgs = [], warns = [], tips = [];
  if (!online) level = "off";
  var b = batt(online ? d.battery_v : null);
  // changes vs the standard settings: information, not a warning - the robot works
  var chg = online ? l.changes || [] : [];
  if (online) {
    if (d.battery_v != null && d.battery_v < 11.0) msgs.push("Batterij leeg - motoren uitgeschakeld door de OpenCR");
    if (l.bringup && l.robot_node === false) msgs.push("OpenCR antwoordt niet (batterij? USB?)");
    if (s.cpu_temp_c > 80) msgs.push("CPU " + Math.round(s.cpu_temp_c) + " °C");
    if (s.throttled & 1) msgs.push("Onderspanning van de Pi (voeding/batterij)");
    if (b[1] === "y") warns.push("batterij bijna leeg");
    if (s.wifi_signal_dbm != null && s.wifi_signal_dbm < -72) warns.push("zwakke wifi");
    health(s, msgs, warns, tips);
    var v = (s.image_version || "").split(" ")[0];
    if (common && v && v !== common) warns.push("andere versie dan de rest (" + common + ")");
    var cc = channelCheck(r);
    if (cc && !cc.ok) warns.push("wifi " + cc.text);
    if (msgs.length) level = "bad"; else if (warns.length) level = "warn";
  }
  var open = !!openTiles[r.robot], st = robotState(l), nr = String(r.nr || "").padStart(2, "0");
  var head = '<div class="head" data-toggle="' + esc(r.robot) + '" title="Klik voor details">' +
    (control ? '<input type="checkbox" data-sel="' + esc(r.robot) + '"' + (selected[r.robot] ? " checked" : "") +
      (r.listening ? "" : " disabled") + ' title="Aanvinken om bij te werken">' : "") +
    '<span class="caret">' + (open ? "▾" : "▸") + "</span><b>" + esc(r.robot) + '</b><span class="sum">' +
    (online ? '<span class="' + st[1] + '">' + st[0] + "</span>" +
      '<span class="hi ' + b[1] + '" title="Batterij ' + esc(b[0]) + '">' + battIcon(b[3]) + b[2] + "</span>" +
      '<span class="hi ' + (s.wifi_signal_dbm == null ? "" : s.wifi_signal_dbm > -60 ? "g" : s.wifi_signal_dbm >= -70 ? "y" : "r") +
        '" title="Wifi ' + (s.wifi_signal_dbm != null ? s.wifi_signal_dbm + " dBm" : "--") + '">' + wifiIcon(s.wifi_signal_dbm) + "</span>" +
      '<span class="hi ' + (s.cpu_temp_c > 80 ? "r" : s.cpu_temp_c > 70 ? "y" : "g") + '" title="Raspberry Pi ' +
        (s.cpu_temp_c != null ? Math.round(s.cpu_temp_c) + " °C" : "--") + '">' + tempIcon() + "</span>" +
      (updateAvailable(s.updates) ? '<span class="hi y" title="Update beschikbaar">' + fxIcon("update", 15) + "</span>" : "")
      : r.seen ? "offline sinds " + ago(age) : "nooit gezien") + "</span></div>";
  var alerts = msgs.map(function (m) { return '<div class="alert">' + esc(m) + "</div>"; }).join("");
  var tip = esc(tips.map(function (t) { return "- " + t; }).join("\n"));
  if (!open)
    return '<div class="tile ' + level + (online ? "" : " off") + '">' + head + alerts +
      (warns.length ? '<div class="warnline" title="' + tip + '">' + esc(warns.join(", ")) + "</div>" : "") + "</div>";
  var temp = s.cpu_temp_c != null ? Math.round(s.cpu_temp_c) + " °C" : "--";
  var tc = s.cpu_temp_c > 80 ? "r" : s.cpu_temp_c > 70 ? "y" : "";
  return '<div class="tile ' + level + (online ? "" : " off") + '">' + head +
    (online ? '<div class="kv">' +
      "<span>Toestand</span><span class=\"" + st[1] + "\">" + st[0] + "</span>" +
      "<span>Batterij</span><span class=\"" + b[1] + "\">" + b[0] + "</span>" +
      "<span>CPU</span><span>" + cpuLoad(s) + ' · <span class="' + tc + '">' + temp + "</span></span>" +
      "<span>Wifi</span><span>" + (s.wifi_signal_dbm != null ? s.wifi_signal_dbm + " dBm" : "--") +
        (s.wifi_band ? '<br><span title="' + s.wifi_freq_mhz + ' MHz">' + esc(s.wifi_band) + " kanaal " + s.wifi_channel +
          (s.wifi_width_mhz ? " · " + s.wifi_width_mhz + " MHz" : "") + "</span>" + channelMark(channelCheck(r)) : "") + "</span>" +
      "<span>Wifi-verkeer</span>" + wifiLoad(s) +
      "<span>Gebruikers</span>" + usersText(s) +
      "<span>Kaart</span><span>" + esc(l.map_running || "--") + "</span>" +
      "<span>USB / lidar</span><span>" + (l.usb_power === "off" ? "uit (spaarstand)" : l.usb_power === "on" ? "aan" : "--") +
        (s.lidar ? ", " + s.lidar.hours.toFixed(0) + " draaiuren" : "") + "</span>" +
      "<span>Software</span><span>" + esc(s.image_version || "--") + softwareState(s.updates) +
        (d.setup_commit ? '<br><span class="sub">setup ' + esc(d.setup_commit) + "</span>" : "") + "</span>" +
      "<span>Uptime</span><span>" + (s.uptime_seconds ? ago(s.uptime_seconds) : "--") + "</span>" +
      "</div>" + alerts +
      (warns.length ? '<div class="warnmsg" title="' + tip + '">' + esc(warns.join(", ")) + "</div>" : "") +
      (chg.length ? '<div class="info" title="' + esc(chg.map(function (c) { return "- " + c.text; }).join("\n")) + '">' +
        chg.length + " aanpassing(en) t.o.v. de standaard (details: muis erop)</div>" : "") + controls(r) : "") +
    '<div class="sub" style="margin-top:8px">' + (KIOSK ? "statuspagina" :
      '<a href="http://10.0.' + Number(r.nr) + '.10:8080/" target="_blank">statuspagina</a>') + " (wifi TB-AP-" + nr + ")</div></div>";
}
// AP channel plan (monitor/wifi_kanalen.csv) vs what the robot reports
// (robot image 1.3.0+, width 1.4.0+): null = nothing to compare
function channelCheck(r) {
  var s = (r.data || {}).status || {}, plan = ((lastJ || {}).wifi_plan || {})[String(Number(r.nr))];
  if (!s.wifi_band || !plan) return null;
  if (s.wifi_band !== "5 GHz")
    return { ok: false, plan: plan, text: "op " + s.wifi_band + " i.p.v. 5 GHz (laptop of robot zonder 5 GHz?)" };
  if (s.wifi_channel !== plan["5"]) return { ok: false, plan: plan, text: "kanaal " + s.wifi_channel + ", gepland " + plan["5"] };
  if (s.wifi_width_mhz && s.wifi_width_mhz !== plan.width)
    return { ok: false, plan: plan, text: s.wifi_width_mhz + " MHz breed, gepland " + plan.width + " MHz" };
  return { ok: true, plan: plan, text: "volgens plan" };
}
function channelMark(c) {
  return !c ? "" : c.ok ? ' <span class="g" title="kanaal volgens plan">✓</span>' : ' <span class="y" title="' + esc(c.text) + '">✗</span>';
}
// Spectrum: every robot's channel as a block (centre ± width/2) on the
// frequency axis, one row per robot - overlapping blocks share airtime.
// Last year's lab: all robots on one network was a disaster.
function spectrum(list, band, opt) {
  opt = opt || {};
  var lo = band === "5 GHz" ? 5170 : 2401, hi = band === "5 GHz" ? 5250 : 2483, step = band === "5 GHz" ? 20 : 5;
  list.forEach(function (x) { lo = Math.min(lo, x.c - x.w / 2); hi = Math.max(hi, x.c + x.w / 2); });
  lo -= step / 2; hi += step / 2;   // room for the outer blocks and labels
  var W = 760, L = opt.L || 40, R = 10, rowH = 18, top = 6, H = top + list.length * rowH + 30;
  var X = function (f) { return L + (f - lo) / (hi - lo) * (W - L - R); };
  var g = "";
  // channel grid: 5 GHz every 20 MHz (36, 40, ...), 2.4 GHz channels 1, 6, 11
  var chans = band === "5 GHz" ? [] : [1, 6, 11, 13];
  if (band === "5 GHz") for (var f = Math.ceil((lo - 5000) / step) * step + 5000; f <= hi; f += step) chans.push((f - 5000) / 5);
  chans.forEach(function (ch) {
    var f = band === "5 GHz" ? 5000 + 5 * ch : 2407 + 5 * ch;
    if (f < lo || f > hi) return;
    g += '<line x1="' + X(f) + '" x2="' + X(f) + '" y1="' + top + '" y2="' + (H - 22) + '" stroke="var(--border)"/>' +
      '<text x="' + X(f) + '" y="' + (H - 8) + '" text-anchor="middle">' + ch + "</text>";
  });
  list.forEach(function (x, i) {
    var y = top + i * rowH, col = x.other ? "var(--muted)" : x.ok === false ? "var(--yellow)" : x.ok ? "var(--green)" : "var(--accent)";
    g += '<rect x="' + X(x.c - x.w / 2) + '" y="' + (y + 2) + '" width="' + Math.max(2, X(x.c + x.w / 2) - X(x.c - x.w / 2)) +
      '" height="' + (rowH - 5) + '" rx="3" fill="' + col + '" fill-opacity="' + (x.other ? ".45" : ".75") + '"><title>' +
      esc(x.name + ": kanaal " + x.ch + ", " + x.w + " MHz" + (x.signal != null ? ", signaal " + x.signal + " %" : "")) +
      '</title></rect><text x="' + (L - 6) + '" y="' + (y + rowH - 5) + '" text-anchor="end">' + esc(x.label) + "</text>";
  });
  return '<div class="sub" style="margin-top:8px">' + band + " - kanaal (onderaan), " + (opt.rows || "één rij per robot") + "</div>" +
    '<svg class="spec" viewBox="0 0 ' + W + " " + H + '">' + g + "</svg>";
}
function renderChannels(on) {
  var bands = {}, rows = [], bad = 0;
  on.forEach(function (r) {
    var s = (r.data || {}).status || {};
    if (!s.wifi_band) return;
    var c = channelCheck(r), w = s.wifi_width_mhz || 20, nr = String(r.nr).padStart(2, "0");
    if (c && !c.ok) bad++;
    (bands[s.wifi_band] = bands[s.wifi_band] || []).push({ name: r.robot, label: nr, ch: s.wifi_channel, w: w,
      c: s.wifi_center_mhz || s.wifi_freq_mhz, ok: c ? c.ok : null });
    rows.push("<tr><td>" + esc(r.robot) + "</td><td>" + esc(s.wifi_band) + "</td><td>" + s.wifi_channel + "</td><td>" +
      (s.wifi_width_mhz ? s.wifi_width_mhz + " MHz" : "?") + "</td><td>" + (c ? c.plan["5"] + " · " + c.plan.width + " MHz" : "--") +
      "</td><td>" + (c ? (c.ok ? '<span class="g">✓ volgens plan</span>' : '<span class="y">✗ ' + esc(c.text) + "</span>") : "") + "</td></tr>");
  });
  var scan = renderScan((lastJ || {}).wifi_scan), box = document.getElementById("chanBox");
  box.style.display = rows.length || scan ? "" : "none";
  if (!rows.length && !scan) return;
  var sum = [];
  if (rows.length) sum.push(bad ? '<span class="y">' + bad + " robot(s) niet volgens plan</span>" : '<span class="g">robots volgens plan</span>');
  if (scan) sum.push(scan.bad ? '<span class="y">' + scan.bad + " AP-instelling(en) niet volgens plan</span>"
    : '<span class="g">AP\'s volgens plan</span>');
  document.getElementById("chanSum").innerHTML = "<b>Wifikanalen</b> " + sum.join(" · ") +
    ' <span class="sub">(' + rows.length + " robot(s) gemeld" + (scan ? ", " + scan.n + " netwerken in de buurt" : "") +
    "; klik voor de grafiek)</span>";
  document.getElementById("chanBody").innerHTML = (rows.length ?
    '<table class="chtab"><tr><th>Robot</th><th>Band</th><th>Kanaal</th><th>Breedte</th><th>Gepland</th><th></th></tr>' +
    rows.join("") + "</table>" +
    Object.keys(bands).sort().reverse().map(function (b) { return spectrum(bands[b], b); }).join("") : "") +
    (scan ? scan.html : "") +
    '<div class="sub" style="margin-top:6px">Plan: monitor/wifi_kanalen.csv. Overlappende blokken delen de zendtijd; ' +
    "20 MHz-kanalen 36/40/44/48 overlappen niet, 80 MHz neemt alle vier in.</div>";
}
// Wifi around the monitor RPi (its free wlan0 scans every minute): the TB-AP
// settings checked against the plan, how busy each planned channel is, and
// every radio as a block (grey = not ours: eduroam, phones, ...).
// Wifitest: robots send data to the monitor for N s. "samen" vs "na elkaar"
// shows how much they slow each other down (shared channel = shared airtime).
function mbps(v) { return v == null ? "--" : String(v).replace(".", ",") + " Mbit/s"; }
function renderWifitest(j) {
  var w = j.wifitest || {}, hist = w.history || [], cur = w.current, box = document.getElementById("wtBox");
  box.style.display = control || hist.length || cur ? "" : "none";
  document.getElementById("wtCtl").style.display = control ? "" : "none";
  document.getElementById("wtStart").disabled = !!w.busy;
  var last = hist[0];
  document.getElementById("wtSum").innerHTML = "<b>Wifitest</b> " + (w.busy ? '<span class="y">bezig (' + cur.mode + ")...</span>"
    : last ? '<span class="sub">laatste: ' + last.mode + ", totaal " + mbps(last.total) + "</span>" : '<span class="sub">nog geen test</span>');
  // newest "samen" and "na elkaar" side by side, plus the running test
  var samen = hist.filter(function (h) { return h.mode === "samen"; })[0],
    alleen = hist.filter(function (h) { return h.mode === "na elkaar"; })[0];
  var names = {};
  [samen, alleen, w.busy ? cur : null].forEach(function (h) { if (h) Object.keys(h.mbps).forEach(function (n) { names[n] = 1; }); });
  var max = 1;
  Object.keys(names).forEach(function (n) {
    [samen, alleen, w.busy ? cur : null].forEach(function (h) { if (h && h.mbps[n]) max = Math.max(max, h.mbps[n]); });
  });
  function bar(v, col) {
    return v == null ? "--" : '<span style="display:inline-block;height:9px;border-radius:2px;vertical-align:middle;margin-right:6px;background:' + col +
      ";width:" + Math.max(2, Math.round(v / max * 160)) + 'px"></span>' + mbps(v);
  }
  function when(h) { return h ? new Date(h.t * 1000).toLocaleTimeString("nl-BE", { hour: "2-digit", minute: "2-digit" }) : ""; }
  var rows = Object.keys(names).sort().map(function (n) {
    return "<tr><td>" + esc(n) + "</td>" + (w.busy ? "<td>" + bar(cur.mbps[n], "var(--yellow)") + "</td>" : "") +
      "<td>" + bar(alleen && alleen.mbps[n], "var(--green)") + "</td><td>" + bar(samen && samen.mbps[n], "var(--accent)") + "</td></tr>";
  });
  var html = rows.length ? '<table class="chtab"><tr><th>Robot</th>' + (w.busy ? "<th>Nu (" + esc(cur.mode) + ")</th>" : "") +
    "<th>Alleen " + (alleen ? '<span class="sub">' + when(alleen) + "</span>" : "") + "</th><th>Samen " +
    (samen ? '<span class="sub">' + when(samen) + "</span>" : "") + "</th></tr>" + rows.join("") +
    "<tr><td><b>Totaal</b></td>" + (w.busy ? "<td></td>" : "") + "<td>--</td><td><b>" + (samen ? mbps(samen.total) : "--") + "</b></td></tr></table>" : "";
  if (hist.length) html += '<div class="sub" style="margin-top:10px">Vorige tests: ' + hist.slice(0, 8).map(function (h) {
    return when(h) + " " + h.mode + " (" + Object.keys(h.mbps).length + " robots, " + h.secs + " s): totaal " + mbps(h.total);
  }).join(" · ") + "</div>";
  html += '<div class="sub" style="margin-top:6px">Gemeten wat er bij de monitor-RPi aankomt (robot → AP → kabel). ' +
    "Alleen = elke robot apart; samen = allemaal tegelijk. Zitten ze op aparte kanalen, dan is samen per robot ongeveer even snel als alleen; " +
    "delen ze een kanaal, dan zakt elke robot weg. Logboek: monitor/wifitest_log.jsonl.</div>";
  document.getElementById("wtBody").innerHTML = html;
}
document.getElementById("wtStart").addEventListener("click", function () {
  var names = selectedNames();
  if (!names.length) names = lastRobots.filter(function (r) { return r.listening; }).map(function (r) { return r.robot; });
  if (!names.length) { alert("Geen enkele robot luistert naar de monitor."); return; }
  var mode = document.getElementById("wtMode").value, secs = Number(document.getElementById("wtSecs").value);
  if (!confirm("Wifitest (" + (mode === "samen" ? "samen" : "na elkaar") + ", " + secs + " s) met " + names.length + " robot(s)?")) return;
  post("/api/wifitest", { robots: names, mode: mode, secs: secs });
});
var wtBox = document.getElementById("wtBox");
try { wtBox.open = localStorage.getItem("tbmonWt") === "1"; } catch (e) {}
wtBox.addEventListener("toggle", function () { try { localStorage.setItem("tbmonWt", wtBox.open ? "1" : "0"); } catch (e) {} });
var LAB_SSIDS = ["Wifi_turtlebots"];   // our switch network: counted as ours, not checked
function renderScan(sc) {
  if (!sc || !sc.radios || !sc.radios.length) return null;
  var plan = (lastJ || {}).wifi_plan || {}, aps = {}, bad = 0, bands = { "5 GHz": [], "2.4 GHz": [] };
  sc.radios.forEach(function (r) {
    var b = r.freq > 5000 ? "5 GHz" : "2.4 GHz", m = /^TB-AP-(\d+)$/.exec(r.ssids[0] || ""), x = {
      name: r.ssids.join(", ") || "(verborgen)", ch: r.ch, w: r.width, c: r.center, signal: r.signal };
    if (LAB_SSIDS.indexOf(r.ssids[0]) >= 0) x.label = r.ssids[0].slice(0, 15);   // lab network: ours, no plan
    else if (m) {
      var nr = String(Number(m[1])), p = plan[nr], want = p ? (b === "5 GHz" ? p["5"] : p["2.4"]) : null,
        ww = p ? (b === "5 GHz" ? p.width : 20) : null;
      x.label = "AP " + m[1]; x.ok = p ? r.ch === want && r.width === ww : null;
      if (x.ok === false) bad++;
      (aps[m[1]] = aps[m[1]] || {})[b] = { r: r, ok: x.ok, want: want, ww: ww };
    } else {
      x.other = true;
      x.label = (r.ssids[0] || "(verborgen)").slice(0, 12) + (r.ssids.length > 1 ? " +" + (r.ssids.length - 1) : "");
    }
    bands[b].push(x);
  });
  function cell(a) {
    if (!a) return "<td>--</td>";
    var t = "kanaal " + a.r.ch + " · " + a.r.width + " MHz";
    return "<td>" + (a.ok === false ? '<span class="y" title="gepland: kanaal ' + a.want + " · " + a.ww + ' MHz">✗ ' + t + "</span>"
      : a.ok ? '<span class="g">✓</span> ' + t : t) + ' <span class="sub">' + a.r.signal + " %</span></td>";
  }
  var aprows = Object.keys(aps).sort().map(function (k) {
    return "<tr><td>TB-AP-" + k + "</td>" + cell(aps[k]["5 GHz"]) + cell(aps[k]["2.4 GHz"]) + "</tr>";
  });
  // busy-ness of each planned channel: radios whose block covers it
  function busy(b, chans) {
    return chans.map(function (ch) {
      var f = b === "5 GHz" ? 5000 + 5 * ch : 2407 + 5 * ch, ours = 0, other = 0, best = 0;
      bands[b].forEach(function (x) {
        if (Math.abs(x.c - f) >= x.w / 2 + (b === "5 GHz" ? 0 : 10)) return;   // 2.4 GHz: 20 MHz channels 5 MHz apart overlap
        if (x.other) { other++; best = Math.max(best, x.signal); } else ours++;
      });
      return "<tr><td>" + b + " kanaal " + ch + "</td><td>" + ours + "</td><td" + (best >= 60 ? ' class="y"' : "") + ">" + other +
        (other ? ' <span class="sub">(sterkste ' + best + " %)</span>" : "") + "</td></tr>";
    }).join("");
  }
  var html = '<div style="margin-top:14px"><b>Omgeving</b> <span class="sub">scan door de monitor-RPi, ' + sc.age +
    " s geleden; signaal 0-100 % zoals de RPi het ontvangt</span></div>" +
    (aprows.length ? '<table class="chtab"><tr><th>AP</th><th>5 GHz</th><th>2.4 GHz</th></tr>' + aprows.join("") + "</table>" : "") +
    '<table class="chtab" style="margin-top:8px"><tr><th>Gepland kanaal</th><th>Eigen netwerken erop</th><th>Andere netwerken erop</th></tr>' +
    busy("5 GHz", [36, 40, 44, 48]) + busy("2.4 GHz", [1, 6, 11]) + "</table>" +
    ["5 GHz", "2.4 GHz"].map(function (b) {
      return bands[b].length ? spectrum(bands[b], b, { L: 110, rows: "één rij per zender (grijs = niet van ons)" }) : "";
    }).join("");
  return { html: html, bad: bad, n: sc.radios.length };
}
var chanBox = document.getElementById("chanBox");
try { chanBox.open = localStorage.getItem("tbmonChan") === "1"; } catch (e) {}
chanBox.addEventListener("toggle", function () { try { localStorage.setItem("tbmonChan", chanBox.open ? "1" : "0"); } catch (e) {} });
function refresh() {
  fetch("/api/robots").then(function (x) { return x.json(); }).then(function (j) {
    lastJ = j;
    render();
  }).catch(function () { document.getElementById("summary").textContent = "monitor niet bereikbaar"; });
}
function render() {
  var j = lastJ;
  if (!j) return;
  var rs = j.robots.slice().sort(function (a, b) { return Number(a.nr) - Number(b.nr); });
  lastRobots = rs; control = j.control;
  var busy = !!(j.update_queue && (j.update_queue.running.length || j.update_queue.waiting.length));
  if (j.update_queue) renderUpdate(j.update_queue);
  var nSel = selectedNames().length, updBtn = document.getElementById("updAll");
  updBtn.disabled = !control || busy || !nSel;
  updBtn.innerHTML = fxIcon("update") + (nSel ? nSel + " robot(s) bijwerken" : "Bijwerken (vink robots aan)");
  document.getElementById("selPick").disabled = !control;
  document.getElementById("rpiPower").style.display = control && j.rpi_power ? "" : "none";
  document.querySelectorAll("[data-all]").forEach(function (b) { b.disabled = !control; });
  var listening = rs.filter(function (r) { return r.listening; }).length;
  document.getElementById("allBar").style.display = j.view_only ? "none" : "";
  var lb = document.getElementById("loginBtn");
  lb.style.display = j.view_only || (control && !j.session) ? "none" : "";
  lb.innerHTML = fxIcon(j.session ? "unlocked" : "beheer") + (j.session ? "Afmelden" : "Docent aanmelden");
  lb.dataset.session = j.session ? "1" : "";
  document.getElementById("openAll").innerHTML = fxIcon("fit") + (rs.some(function (r) { return !openTiles[r.robot]; }) ? "Alles open" : "Alles dicht");
  document.getElementById("ctrlInfo").textContent = !control ? (j.pin_set ? "knoppen na Docent aanmelden"
      : "knoppen: stel eerst een docenten-PIN in op de monitor zelf")
    : listening + " robot(s) luisteren naar opdrachten";
  if (document.activeElement && document.activeElement.tagName === "SELECT") return;   // don't close an open menu
  // most common software version of the online robots (whole class on one version)
  var on = rs.filter(function (r) { return r.seen && j.now - r.seen < 20; });
  var upd = on.filter(function (r) { return updateAvailable(((r.data || {}).status || {}).updates); }).length;
  var count = {}, common = "";
  on.forEach(function (r) { var v = (((r.data || {}).status || {}).image_version || "").split(" ")[0]; if (v) count[v] = (count[v] || 0) + 1; });
  Object.keys(count).forEach(function (v) { if (!common || count[v] > count[common]) common = v; });
  if (Object.keys(count).length < 2) common = "";
  // all robots together: each has its own AP, but the APs share the 2.4 GHz band in one room
  var rx = 0, tx = 0, pages = 0, haveWeb = false;
  on.forEach(function (r) {
    var s = (r.data || {}).status || {}; rx += netRx(s) || 0; tx += netTx(s) || 0;
    if (s.web_clients) { haveWeb = true; pages += s.web_clients.pages; }
  });
  document.getElementById("grid").innerHTML = rs.map(function (r) { return tile(r, j.now, common); }).join("");
  renderChannels(on);
  renderWifitest(j);
  var bad = document.querySelectorAll(".tile.bad").length, warn = document.querySelectorAll(".tile.warn").length;
  document.getElementById("summary").textContent = on.length + " van " + rs.length + " robots online" +
    (bad ? ", " + bad + " met een probleem" : "") + (warn ? ", " + warn + " met aandacht" : "") +
    (upd ? ", " + upd + " met een update beschikbaar" : "") +
    (on.length ? " · wifi-verkeer samen ↓ " + rate(rx) + " · ↑ " + rate(tx) : "") +
    (haveWeb ? " · " + pages + " pagina's open" : "") + " - " + new Date().toLocaleTimeString("nl-BE");
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
    ap.add_argument("--set-pin", action="store_true",
                    help="docenten-PIN van de monitor instellen (Docent aanmelden) en stoppen")
    ap.add_argument("--control-port", type=int, default=0,
                    help="tweede poort waarop de knoppen altijd werken (Docker: enkel op 127.0.0.1 publiceren)")
    args = ap.parse_args()
    if args.set_pin:
        import getpass
        pin = getpass.getpass("Nieuwe docenten-PIN (4-8 cijfers): ")
        if not (pin.isdigit() and 4 <= len(pin) <= 8) or getpass.getpass("Nog eens: ") != pin:
            raise SystemExit("Niet ingesteld: 4 tot 8 cijfers, twee keer hetzelfde.")
        save_pin(pin_hash(pin))
        raise SystemExit("Docenten-PIN ingesteld (%s)." % PIN_FILE)
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
