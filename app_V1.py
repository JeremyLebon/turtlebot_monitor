from flask import Flask, render_template, jsonify
import nmap
import json
import time
import threading
import subprocess
import re
import socket

# ROS2 imports
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import BatteryState

app = Flask(__name__)
scanner = nmap.PortScanner()

SCAN_SUBNET = "192.168.60.0/24"
SCAN_INTERVAL = 10  # seconden

# ---- Load TurtleBots from JSON ----
with open("turtlebots.json") as f:
    TURTLEBOTS = json.load(f)["turtlebots"]   # voorbeeld: { "1": "AA:BB:...", "2": "CC:DD:..." }

status_data = {
    "turtlebots": {},
    "others": {}
}

# ==========================================================
# ROS2 MULTI BATTERY MONITOR
# ==========================================================

class MultiBatteryMonitor(Node):
    def __init__(self, turtlebot_names):
        super().__init__("battery_monitor_multi")
        self.subscribers = []
        number_turtlebot = 0
        for name in turtlebot_names:
            topic = f"/battery_state_{number_turtlebot}"
            number_turtlebot +=1
            self.get_logger().info(f"Subscribing to: {topic}")

            sub = self.create_subscription(
                BatteryState,
                topic,
                lambda msg, robot=name: self.listener_callback(msg, robot),
                10
            )
            self.subscribers.append(sub)

    def listener_callback(self, msg, robot_name):
        # Voeg batterij data toe
        if robot_name in status_data["turtlebots"]:
            status_data["turtlebots"][robot_name]["battery"] = {
                "voltage": msg.voltage,
                "current": msg.current,
                "percentage": msg.percentage,
            }
        else:
            # fallback voor het geval dat netscan de robot nog niet heeft gezien
            status_data["turtlebots"][robot_name] = {
                "battery": {
                    "voltage": msg.voltage,
                    "current": msg.current,
                    "percentage": msg.percentage,
                }
            }


def ros2_spin_thread():
    rclpy.init()

    turtlebot_names = list(TURTLEBOTS.keys())  # bijv. ["1", "2", "3"]

    node = MultiBatteryMonitor(turtlebot_names)

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass

    node.destroy_node()
    rclpy.shutdown()


# ==========================================================
# NETWORK SCANNER THREAD
# ==========================================================

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

        # check alle turtlebots
        for name, mac in TURTLEBOTS.items():
            mac = mac.upper()

            if mac in found_devices:
                known[name] = {
                    "mac": mac,
                    "ip": found_devices[mac]["ip"],
                    "status": "online",
                    "battery": status_data.get("turtlebots", {})
                                    .get(name, {})
                                    .get("battery", None)
                }
            else:
                known[name] = {
                    "mac": mac,
                    "ip": None,
                    "status": "offline",
                    "battery": status_data.get("turtlebots", {})
                                    .get(name, {})
                                    .get("battery", None)
                }

        # onbekende apparaten
        for mac, data in found_devices.items():
            if mac not in [m.upper() for m in TURTLEBOTS.values()]:
                others[mac] = data

        status_data["turtlebots"] = known
        status_data["others"] = others

        time.sleep(SCAN_INTERVAL)


# ==========================================================
# FLASK ROUTES
# ==========================================================

@app.route("/")
def index():
    return render_template("index_V1.html", data=status_data["turtlebots"])

@app.route("/others")
def others():
    return render_template("others_V1.html", data=status_data["others"])

@app.route("/api/status")
def api_status():
    return jsonify(status_data)


# ==========================================================
# MAIN
# ==========================================================

if __name__ == "__main__":
    # Start netwerk scanner
    scan_thread = threading.Thread(target=scan_network, daemon=True)
    scan_thread.start()

    # Start ROS2 battery monitor
    ros_thread = threading.Thread(target=ros2_spin_thread, daemon=True)
    ros_thread.start()

    app.run(host="0.0.0.0", port=5000)
