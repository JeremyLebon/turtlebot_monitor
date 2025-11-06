import os
import json
import time
import threading
from flask import Flask, render_template_string, jsonify, request
import nmap
import socket

# Config via env of file
SUBNET = os.environ.get("SCAN_SUBNET", "192.168.60.0/24")
INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))
TURTLEBOTS_FILE = "turtlebots.json"

# load turtlebot macs
try:
    with open(TURTLEBOTS_FILE, "r") as f:
        cfg = json.load(f)
        TURTLEBOT_MACS = {k: v.upper() for k, v in cfg.get("turtlebots", {}).items()}
except Exception:
    TURTLEBOT_MACS = {}

app = Flask(__name__)
scanner = nmap.PortScanner()
state = {
    "last_scan": None,
    "devices": [],   # list of dicts: ip, mac, vendor, hostname
    "turtlebots": {} # host -> details if recognized
}

def scan_network(subnet=SUBNET):
    devices = []
    try:
        # -sn: ping/host discovery only
        scanner.scan(hosts=subnet, arguments='-sn')
        for host in scanner.all_hosts():
            ip = host
            mac = None
            vendor = None
            hostname = None
            # addresses may be available in scanner[host]['addresses']
            if 'addresses' in scanner[host]:
                mac = scanner[host]['addresses'].get('mac')
            # vendor info
            if 'vendor' in scanner[host] and mac:
                vendor = scanner[host]['vendor'].get(mac)
            # try hostname resolution
            try:
                hostname = scanner[host].hostname()
                if hostname == '':
                    # fallback to reverse DNS
                    try:
                        hostname = socket.gethostbyaddr(ip)[0]
                    except Exception:
                        hostname = None
            except Exception:
                hostname = None
            devices.append({
                "ip": ip,
                "mac": (mac.upper() if mac else None),
                "vendor": vendor,
                "hostname": hostname
            })
    except Exception as e:
        # scanner errors (e.g., permissions) are passed up as empty list
        print("Scan error:", e)
    return devices

def identify_turtlebots(devices):
    found = {}
    # normalize turtlemac values for matching (allow partial OUI matches)
    mac_values = [m.upper() for m in TURTLEBOT_MACS.values()]
    for d in devices:
        if d['mac']:
            mac = d['mac'].upper()
            for host, tmac in TURTLEBOT_MACS.items():
                tmac_up = tmac.upper()
                # exact or prefix match
                if mac == tmac_up or mac.startswith(tmac_up):
                    found[host] = {
                        "ip": d['ip'],
                        "mac": d['mac'],
                        "vendor": d.get('vendor'),
                        "hostname": d.get('hostname')
                    }
    return found

def background_worker():
    while True:
        devices = scan_network()
        tbs = identify_turtlebots(devices)
        state['devices'] = devices
        state['turtlebots'] = tbs
        state['last_scan'] = time.time()
        time.sleep(max(1, INTERVAL))

@app.route("/")
def index():
    html = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>TurtleBot Monitor</title>
      <meta http-equiv="refresh" content="10">
      <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        table { border-collapse: collapse; width: 90%; margin-bottom: 30px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background: #f2f2f2; }
        .online { color: green; font-weight: bold; }
        .offline { color: red; font-weight: bold; }
      </style>
    </head>
    <body>
      <h1>TurtleBot & WiFi Devices</h1>
      <h2>Herkenbare TurtleBots</h2>
      <table>
        <tr><th>Configured Host</th><th>IP</th><th>MAC</th><th>Vendor</th><th>Hostname</th></tr>
        {% for host, info in turtlebots.items() %}
          <tr>
            <td>{{ host }}</td>
            <td>{{ info.ip }}</td>
            <td>{{ info.mac }}</td>
            <td>{{ info.vendor or '' }}</td>
            <td>{{ info.hostname or '' }}</td>
          </tr>
        {% else %}
          <tr><td colspan="5">Geen TurtleBots gedetecteerd</td></tr>
        {% endfor %}
      </table>

      <h2>Alle apparaten op {{ subnet }}</h2>
      <table>
        <tr><th>IP</th><th>Hostname</th><th>MAC</th><th>Vendor</th></tr>
        {% for d in devices %}
        <tr>
          <td>{{ d.ip }}</td>
          <td>{{ d.hostname or '' }}</td>
          <td>{{ d.mac or '' }}</td>
          <td>{{ d.vendor or '' }}</td>
        </tr>
        {% endfor %}
      </table>

      <p>Laatste scan: {{ last_scan_str }}</p>
    </body>
    </html>
    """
    last_scan = state.get('last_scan')
    if last_scan:
        last_scan_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_scan))
    else:
        last_scan_str = "nog niet gescand"
    return render_template_string(html,
                                  turtlebots=state.get('turtlebots', {}),
                                  devices=state.get('devices', []),
                                  last_scan_str=last_scan_str,
                                  subnet=SUBNET)

@app.route("/api/status")
def api_status():
    return jsonify({
        "subnet": SUBNET,
        "last_scan": state.get('last_scan'),
        "devices": state.get('devices'),
        "turtlebots": state.get('turtlebots')
    })

@app.route("/api/scan_now", methods=["POST"])
def api_scan_now():
    # trigger immediate scan once
    devices = scan_network()
    tbs = identify_turtlebots(devices)
    state['devices'] = devices
    state['turtlebots'] = tbs
    state['last_scan'] = time.time()
    return jsonify({"msg": "scanned", "turtlebots_found": list(tbs.keys())})

if __name__ == "__main__":
    # start background thread
    t = threading.Thread(target=background_worker, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("FLASK_RUN_PORT", 5000)))
