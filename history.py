"""Historiek van de vloot: statuswaarden en gebeurtenissen per robot (SQLite).

De robots sturen elke 5 s hun status naar de monitor (/push). Hier bewaren we:
  - samples: elke 10 s per robot een rij met de waarden die we in grafieken
    tonen (batterij, wifi, CPU, temperatuur, verkeer, toestand). 7 dagen in
    detail, daarna 1 rij per minuut, na 30 dagen weg.
  - events: overgangen die de monitor zelf afleidt uit opeenvolgende berichten
    (offline/online, Pi herstart, onderspanning, batterij, wifi, toestand,
    diensten, OpenCR/USB, ROS-fouten, software bijgewerkt). 30 dagen.
Zo blijft ook bewaard wat er gebeurde vlak voor een robot uitviel.

Enkel standaardbibliotheek (sqlite3); het bestand history.db staat naast
monitor.py en zit niet in git.
"""
import json
import os
import re
import sqlite3
import threading
import time

SAMPLE_EVERY_S = 10       # per robot hoogstens 1 sample per 10 s
DETAIL_DAYS = 7           # daarna 1 sample per minuut
KEEP_DAYS = 30
OFFLINE_AFTER_S = 20      # zelfde grens als de tegels

SVC_NAME = {"zenoh": "Zenoh", "rosbridge": "rosbridge", "webpagina": "webpagina", "launch_control": "launch control",
            "twist_mux": "twist_mux", "cmd_vel_guard": "cmd_vel_guard", "nav_web": "nav-web", "system_info": "systeeminfo"}

COLUMNS = ("battery_v", "cpu_temp", "cpu_use", "load1", "wifi_dbm", "wifi_ch", "rx_kbps", "tx_kbps",
           "throttled", "state", "uptime")


def _state(launch):
    if not launch.get("bringup"):
        return "inactief"
    if launch.get("navigation"):
        return "navigatie"
    if launch.get("slam"):
        return "kaart maken"
    return "bringup"


def _num(v, nd=2):
    try:
        return None if v is None else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def extract(payload):
    """The values we keep from one /push payload."""
    s = payload.get("status") or {}
    launch = payload.get("launch") or {}
    cores = s.get("cpu_percent_per_core") or []
    rx = s.get("net_rx_kbps_avg", s.get("net_rx_kbps"))
    tx = s.get("net_tx_kbps_avg", s.get("net_tx_kbps"))
    thr = s.get("throttled")
    return {
        "battery_v": _num(payload.get("battery_v")),
        "cpu_temp": _num(s.get("cpu_temp_c"), 1),
        "cpu_use": _num(sum(cores) / len(cores), 1) if cores else None,
        "load1": _num(s.get("cpu_load_1min")),
        "wifi_dbm": _num(s.get("wifi_signal_dbm"), 0),
        "wifi_ch": s.get("wifi_channel") if isinstance(s.get("wifi_channel"), int) else None,
        "rx_kbps": _num(rx, 0),
        "tx_kbps": _num(tx, 0),
        "throttled": thr if isinstance(thr, int) else None,
        "state": _state(launch),
        "uptime": _num(s.get("uptime_seconds"), 0),
    }


class History:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")      # SD card: fewer fsyncs
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (robot TEXT, t REAL, %s)"
                        % ", ".join(c + (" TEXT" if c == "state" else " REAL") for c in COLUMNS))
        self.db.execute("CREATE INDEX IF NOT EXISTS samples_rt ON samples (robot, t)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (robot TEXT, t REAL, kind TEXT, level TEXT, text TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_t ON events (t)")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_rt ON events (robot, t)")
        self.db.commit()
        self.prev = {}         # robot -> {"t", "v": extract(), "raw": payload, "online": bool}
        self.flags = {}        # (robot, key) -> {"on": reported?, "since": time the condition flipped}
        self.last_sample = {}  # robot -> time of the last stored sample
        self.last_prune = 0
        self._dirty = False

    # ------------------------------------------------------------- writing
    def event(self, robot, kind, level, text, t=None):
        text = re.sub(r"(\d)\.(\d+ V)", r"\1,\2", text)     # 11.36 V -> 11,36 V
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (robot, t or time.time(), kind, level, text[:300]))
            self._dirty = True

    def on_push(self, robot, payload, now=None):
        """Called for every /push: store a sample (max 1 per 10 s) and
        derive events by comparing with the previous message."""
        now = now or time.time()
        v = extract(payload)
        p = self.prev.get(robot)
        try:
            self._events(robot, p, v, payload, now)
        except Exception as exc:     # never let a strange payload break /push
            print("historiek: %s: %s" % (robot, exc))
        self.prev[robot] = {"t": now, "v": v, "raw": payload, "online": True}
        if now - self.last_sample.get(robot, 0) >= SAMPLE_EVERY_S - 0.5:
            self.last_sample[robot] = now
            with self.lock:
                self.db.execute("INSERT INTO samples VALUES (?,?,%s)" % ",".join("?" * len(COLUMNS)),
                                (robot, now) + tuple(v[c] for c in COLUMNS))
                self._dirty = True

    def _events(self, robot, p, v, payload, now):
        s = payload.get("status") or {}
        launch = payload.get("launch") or {}
        up = v["uptime"]
        rebooted = p is not None and up is not None and p["v"]["uptime"] is not None and up + 30 < p["v"]["uptime"]
        if p is None or not p.get("online"):
            # p is None: first message since the monitor started; only an event
            # when the robot was offline before (otherwise every restart of the
            # monitor would log 9 x "online")
            if p is not None or self._last_event_kind(robot) == "offline":
                gone = "" if p is None else " (na %s offline)" % _dur(now - p["t"])
                self.event(robot, "online", "info", "Online" + gone, now)
            if rebooted or (up is not None and up < 300):
                self.event(robot, "boot", "warn", "Raspberry Pi is opgestart (uptime %s)" % _dur(up), now)
            if p is None:
                return
        elif rebooted:     # rebooted between two messages (< 20 s offline)
            self.event(robot, "boot", "warn", "Raspberry Pi herstart (uptime %s)" % _dur(up), now)
        pv, ps, pl = p["v"], (p["raw"].get("status") or {}), (p["raw"].get("launch") or {})
        # software version
        a, b = ps.get("image_version"), s.get("image_version")
        if a and b and a != b:
            self.event(robot, "software", "info", "Software: %s -> %s" % (a, b), now)
        # under-voltage: bit 0 = now, bit 16 = since boot
        thr, pthr = v["throttled"] or 0, pv["throttled"] or 0
        if thr & 1 and not pthr & 1:
            self.event(robot, "undervoltage", "error", "Onderspanning van de Pi (nu)", now)
        if thr & 0x10000 and not pthr & 0x10000:
            self.event(robot, "undervoltage", "error", "Onderspanning gemeten sinds de opstart (get_throttled 0x%x)" % thr, now)
        if thr & 4 and not pthr & 4:
            self.event(robot, "throttle", "warn", "Pi vertraagt (throttled)", now)
        # battery
        bv, pbv = v["battery_v"], pv["battery_v"]
        if bv is not None:
            # sustained: the voltage sags while driving and jumps back when idle
            if self._sustained(robot, "bat_empty", bv < 11.0, bv > 11.3, now, hold=10) == "start":
                self.event(robot, "battery", "error", "Batterij leeg (< 11,0 V): motoren uit, %.2f V" % bv, now)
            r = self._sustained(robot, "bat_low", bv < 11.4, bv > 11.7, now)
            if r == "start":
                self.event(robot, "battery", "warn", "Batterij bijna leeg (30 s onder 11,4 V, nu %.2f V)" % bv, now)
        if bv is not None and pbv is not None:
            if bv > pbv + 0.6 and pbv < 11.8:
                self.event(robot, "battery", "info", "Batterij gewisseld of opgeladen: %s -> %s V" % (("%.2f" % pbv).replace(".", ","), ("%.2f" % bv).replace(".", ",")), now)
        if (bv is None) != (pbv is None) and launch.get("bringup") and pl.get("bringup"):
            self.event(robot, "battery", "warn", "Geen batterijspanning meer van de OpenCR" if bv is None
                       else "Batterijspanning terug: %.2f V" % bv, now)
        # wifi
        w = v["wifi_dbm"]
        if w is not None:
            r = self._sustained(robot, "wifi", w < -72, w >= -68, now)
            if r == "start":
                self.event(robot, "wifi", "warn", "Zwakke wifi (30 s onder -72 dBm, nu %d dBm)" % w, now)
            elif r == "end":
                self.event(robot, "wifi", "info", "Wifi terug goed: %d dBm" % w, now)
        if v["wifi_ch"] and pv["wifi_ch"] and v["wifi_ch"] != pv["wifi_ch"]:
            self.event(robot, "wifi", "warn", "Wifikanaal %s -> %s" % (pv["wifi_ch"], v["wifi_ch"]), now)
        # temperature
        tc = v["cpu_temp"]
        if tc is not None:
            r = self._sustained(robot, "temp", tc > 80, tc < 75, now)
            if r == "start":
                self.event(robot, "temp", "warn", "CPU warm: %.0f °C" % tc, now)
            elif r == "end":
                self.event(robot, "temp", "info", "CPU terug koeler: %.0f °C" % tc, now)
        # state (bringup / map / navigation)
        if v["state"] != pv["state"]:
            self.event(robot, "state", "info", "Toestand: %s -> %s" % (pv["state"], v["state"]), now)
        # OpenCR / devices (only meaningful while bringup runs)
        if launch.get("bringup") and pl.get("bringup"):
            if launch.get("robot_node") is False and pl.get("robot_node") is not False:
                self.event(robot, "opencr", "error", "OpenCR antwoordt niet (batterij? USB?)", now)
            elif launch.get("robot_node") and pl.get("robot_node") is False:
                self.event(robot, "opencr", "info", "OpenCR antwoordt terug", now)
        for key, name in (("joystick_device", "Gamepad-ontvanger"), ("camera_sensor", "Camera")):
            a, b = pl.get(key), launch.get(key)
            if a is True and b is False:
                self.event(robot, "usb", "warn", "%s niet meer gevonden" % name, now)
            elif a is False and b is True:
                self.event(robot, "usb", "info", "%s terug gevonden" % name, now)
        if pl.get("usb_power") and launch.get("usb_power") and pl.get("usb_power") != launch.get("usb_power"):
            self.event(robot, "usb", "info", "USB-voeding %s" % ("aan" if launch.get("usb_power") == "on" else "uit"), now)
        # services and crashes (supervisor)
        sv, psv = s.get("services") or {}, ps.get("services") or {}
        for k, up in sv.items():
            if k == "terminal" or k not in psv:
                continue
            if up is False and psv[k] is not False:
                self.event(robot, "service", "error", "Dienst gestopt: %s" % SVC_NAME.get(k, k), now)
            elif up is not False and psv[k] is False:
                self.event(robot, "service", "info", "Dienst draait terug: %s" % SVC_NAME.get(k, k), now)
        sup, psup = s.get("supervisor") or {}, ps.get("supervisor") or {}
        for k, info in sup.items():
            last = (info or {}).get("last")
            if last and last != (psup.get(k) or {}).get("last"):
                self.event(robot, "crash", "warn", "%s gecrasht en herstart" % SVC_NAME.get(k, k), now)
        # new ROS errors (by robot time + node)
        seen = {(e.get("t"), e.get("node")) for e in ps.get("rosout_errors") or []}
        for e in s.get("rosout_errors") or []:
            if (e.get("t"), e.get("node")) not in seen:
                self.event(robot, "ros", "error" if e.get("level") == "FATAL" else "warn",
                           "%s %s: %s" % (e.get("level", "ERROR"), e.get("node", "?"), e.get("msg", "")), now)

    def _sustained(self, robot, key, bad, good, now, hold=30):
        """Debounce for noisy values (wifi, temperature): "start" once `bad`
        held for `hold` s, "end" once `good` held for `hold` s after a start."""
        f = self.flags.setdefault((robot, key), {"on": False, "since": None})
        want = good if f["on"] else bad
        if not want:
            f["since"] = None
            return None
        if f["since"] is None:
            f["since"] = now
        if now - f["since"] >= hold:
            f["on"], f["since"] = not f["on"], None
            return "start" if f["on"] else "end"
        return None

    def _last_event_kind(self, robot):
        with self.lock:
            r = self.db.execute("SELECT kind FROM events WHERE robot=? ORDER BY t DESC LIMIT 1", (robot,)).fetchone()
        return r[0] if r else None

    def tick(self, now=None):
        """Every few seconds (thread): offline detection, commit, pruning."""
        now = now or time.time()
        for robot, p in list(self.prev.items()):
            if p.get("online") and now - p["t"] > OFFLINE_AFTER_S:
                p["online"] = False
                v = p["v"]
                extra = []
                if v["battery_v"] is not None:
                    extra.append("batterij %.2f V" % v["battery_v"])
                if v["wifi_dbm"] is not None:
                    extra.append("wifi %d dBm" % v["wifi_dbm"])
                if v["throttled"]:
                    extra.append("throttled 0x%x" % v["throttled"])
                self.event(robot, "offline", "error",
                           "Offline (laatste bericht: %s, toestand %s)" % (", ".join(extra) or "geen waarden", v["state"]),
                           p["t"])
        with self.lock:
            if self._dirty:
                self.db.commit()
                self._dirty = False
            if now - self.last_prune > 3600:
                self.last_prune = now
                old, detail = now - KEEP_DAYS * 86400, now - DETAIL_DAYS * 86400
                self.db.execute("DELETE FROM samples WHERE t < ?", (old,))
                self.db.execute("DELETE FROM events WHERE t < ?", (old,))
                self.db.execute("DELETE FROM samples WHERE t < ? AND rowid NOT IN "
                                "(SELECT MIN(rowid) FROM samples WHERE t < ? GROUP BY robot, CAST(t / 60 AS INTEGER))",
                                (detail, detail))
                self.db.commit()

    # ------------------------------------------------------------- reading
    def samples(self, robot, since, until=None, max_points=720):
        until = until or time.time()
        with self.lock:
            rows = self.db.execute("SELECT t, %s FROM samples WHERE robot=? AND t>=? AND t<=? ORDER BY t"
                                   % ", ".join(COLUMNS), (robot, since, until)).fetchall()
        if len(rows) > max_points:      # thin out evenly, keep the last one
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points - 1)] + [rows[-1]]
        return {"columns": ("t",) + COLUMNS, "rows": rows}

    def events(self, since, robot=None, limit=500):
        q, args = "SELECT robot, t, kind, level, text FROM events WHERE t>=?", [since]
        if robot:
            q += " AND robot=?"
            args.append(robot)
        q += " ORDER BY t DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [dict(zip(("robot", "t", "kind", "level", "text"), r)) for r in rows]

    def csv(self, robot, since):
        d = self.samples(robot, since, max_points=10 ** 9)
        out = [";".join(("tijd",) + COLUMNS)]
        for r in d["rows"]:
            out.append(";".join([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0]))] +
                                [_cell(x) for x in r[1:]]))
        return "\n".join(out) + "\n"

    def size_mb(self):
        try:
            return round(sum(os.path.getsize(self.path + x) for x in ("", "-wal") if os.path.exists(self.path + x)) / 1e6, 1)
        except OSError:
            return None


def _cell(x):
    if x is None:
        return ""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x).replace(".", ",")


def _dur(sec):
    sec = int(sec)
    if sec < 90:
        return "%d s" % sec
    if sec < 5400:
        return "%d min" % round(sec / 60)
    if sec < 172800:
        return "%.1f u" % (sec / 3600)
    return "%d dagen" % round(sec / 86400)


if __name__ == "__main__":       # quick look: python3 history.py [robot]
    import sys
    h = History(os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db"))
    for e in reversed(h.events(time.time() - 86400, sys.argv[1] if len(sys.argv) > 1 else None, 50)):
        print(time.strftime("%d-%m %H:%M:%S", time.localtime(e["t"])), e["robot"], e["level"], e["text"])
    print(json.dumps({"size_mb": h.size_mb()}))
