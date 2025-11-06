from flask import Flask, render_template, jsonify
import nmap
import json
import time
import threading
import subprocess
import re
import socket

app = Flask(__name__)
scanner = nmap.PortScanner()

SCAN_SUBNET = "192.168.60.0/24"
SCAN_INTERVAL = 10  # seconden

# Data laden
with open("turtlebots.json") as f:
    TURTLEBOTS = json.load(f)["turtlebots"]

status_data = {
    "turtlebots": {},
    "others": {}
}

def get_arp_table():
    """Leest lokale ARP-tabel uit"""
    arp_output = subprocess.getoutput("arp -n")
    arp_entries = {}
    for line in arp_output.splitlines()[1:]:
        parts = re.split(r"\s+", line)
        if len(parts) >= 3:
            ip = parts[0]
            mac = parts[2].upper()
            arp_entries[ip] = mac
    return arp_entries


def resolve_hostname(ip):
    """Probeert hostname te vinden via reverse DNS"""
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


def scan_network():
    while True:
        print("Scanning network...")
        scanner.scan(hosts=SCAN_SUBNET, arguments="-sn")

        arp_table = get_arp_table()
        found_devices = {}

        for ip, mac in arp_table.items():
            hostname = resolve_hostname(ip)
            found_devices[mac] = {
                "ip": ip,
                "hostname": hostname
            }

        known = {}
        others = {}

        for name, mac in TURTLEBOTS.items():
            if mac in found_devices:
                known[name] = {
                    "mac": mac,
                    "ip": found_devices[mac]["ip"],
                    "hostname": found_devices[mac]["hostname"],
                    "status": "online"
                }
            else:
                known[name] = {
                    "mac": mac,
                    "ip": None,
                    "hostname": None,
                    "status": "offline"
                }

        for mac, data in found_devices.items():
            if mac not in TURTLEBOTS.values():
                others[mac] = data

        status_data["turtlebots"] = known
        status_data["others"] = others

        time.sleep(SCAN_INTERVAL)

@app.route("/")
def index():
    return render_template("index.html", data=status_data["turtlebots"])

@app.route("/others")
def others():
    return render_template("others.html", data=status_data["others"])

@app.route("/api/status")
def api_status():
    return jsonify(status_data)

if __name__ == "__main__":
    thread = threading.Thread(target=scan_network, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=5000)
