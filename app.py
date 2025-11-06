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

def scan_network():
    while True:
        print("Scanning network...")
        result = scanner.scan(hosts=SCAN_SUBNET, arguments="-sn")
        found_devices = {}
        for host in result["scan"]:
            info = result["scan"][host]
            mac = info.get("addresses", {}).get("mac", "").upper()
            hostname = info.get("hostnames", [{}])[0].get("name", "")
            if mac:
                found_devices[mac] = {"ip": host, "hostname": hostname}

        known = {}
        others = {}

        for name, mac in TURTLEBOTS.items():
            if mac in found_devices:
                known[name] = {
                    "mac": mac,
                    "ip": found_devices[mac]["ip"],
                    "status": "online"
                }
            else:
                known[name] = {
                    "mac": mac,
                    "ip": None,
                    "status": "offline"
                }

        # onbekende apparaten
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
