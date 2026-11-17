#!/usr/bin/env python3
"""fleet_node - de robot in de vloot (CONCEPT, hoort in turtlebot_docker bin/).

Draait naast Navigatie. Zolang Navigatie loopt met een kaart (dezelfde kaart uit
de kaartenbibliotheek op alle robots = hetzelfde map-frame):

  * stuurt hij 4x/s zijn positie (TF map -> base_footprint), snelheid, doel en
    gepland pad naar de monitor: POST <monitor>/fleet;
  * krijgt hij in het antwoord de andere robots op dezelfde kaart terug en
    publiceert die als
      /fleet/robots     visualization_msgs/MarkerArray  (RViz, Lichtblick, nav.html)
      /fleet/poses      geometry_msgs/PoseArray
      /fleet/obstacles  sensor_msgs/PointCloud2  (ring per robot, voor een
                        observation source in de costmap - fase 2)

Waarom via de monitor en niet via Zenoh/DDS: elke robot zit achter de NAT van
zijn eigen AP; de monitor is het enige dat ze allemaal bereiken (zoals
system_info_node met /push). Geen extra pakketten: urllib + rclpy.

Omgeving: MONITOR_URL (zelfde als system_info_node, ".../push"), TURTLEBOT_NR.
"""
import json
import math
import os
import socket
import threading
import time
import urllib.request

import rclpy
from geometry_msgs.msg import Pose, PoseArray, PoseWithCovarianceStamped
from nav_msgs.msg import Odometry, Path
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2
from std_msgs.msg import ColorRGBA, Header
from tf2_ros import Buffer, TransformListener
from visualization_msgs.msg import Marker, MarkerArray

try:                                   # zelfde map als map_bundle.py op de robot
    from map_bundle import running_map
except ImportError:                    # concept los getest: kaartnaam via parameter
    running_map = None

# zelfde kleuren als de vlootkaart op de monitor (index = nr)
COLORS = ["#58a6ff", "#3fb950", "#e3b341", "#f778ba", "#a371f7", "#ff7b38", "#39c5cf", "#d2a8ff", "#7ee787", "#ffa198"]


def color(nr, a=1.0):
    try:
        h = COLORS[(int(nr) + 9) % len(COLORS)]
    except (TypeError, ValueError):
        h = COLORS[9]
    return ColorRGBA(r=int(h[1:3], 16) / 255, g=int(h[3:5], 16) / 255, b=int(h[5:7], 16) / 255, a=a)


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class FleetNode(Node):
    def __init__(self):
        super().__init__("fleet")
        url = os.environ.get("MONITOR_URL", "").strip()
        self.url = (url[:-len("/push")] if url.endswith("/push") else url.rstrip("/")) + "/fleet" if url else ""
        self.robot = socket.gethostname()
        self.nr = os.environ.get("TURTLEBOT_NR", "")
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.base_frame = self.declare_parameter("base_frame", "base_footprint").value
        self.map_param = self.declare_parameter("map_name", "").value
        self.radius = self.declare_parameter("robot_radius", 0.14).value      # Burger ~0.14 m
        rate = self.declare_parameter("rate", 4.0).value

        self.tf = Buffer()
        TransformListener(self.tf, self)
        self.v = 0.0
        self.plan = None                # (time, [[x, y], ...])
        self.loc_ok = True
        self.map_name, self.map_checked = None, 0.0
        self.others, self.others_t = [], 0.0
        self.busy, self.backoff = False, 0.0

        self.create_subscription(Odometry, "/odom", lambda m: setattr(self, "v", m.twist.twist.linear.x), 10)
        self.create_subscription(Path, "/plan", self._on_plan, 1)
        amcl_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                              reliability=ReliabilityPolicy.RELIABLE)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._on_amcl, amcl_qos)
        self.pub_markers = self.create_publisher(MarkerArray, "/fleet/robots", 1)
        self.pub_poses = self.create_publisher(PoseArray, "/fleet/poses", 1)
        self.pub_cloud = self.create_publisher(PointCloud2, "/fleet/obstacles", 1)
        self.create_timer(1.0 / rate, self._tick)
        if not self.url:
            self.get_logger().warn("MONITOR_URL leeg: geen vloot")

    # --- eigen toestand ---------------------------------------------------
    def _on_plan(self, msg):
        pts = [[p.pose.position.x, p.pose.position.y] for p in msg.poses]
        step = max(1, len(pts) // 40)
        self.plan = (time.monotonic(), pts[::step] + pts[-1:]) if pts else None

    def _on_amcl(self, msg):
        c = msg.pose.covariance
        self.loc_ok = math.sqrt(max(0.0, c[0] + c[7])) < 0.25     # ~25 cm onzekerheid

    def _current_map(self):
        now = time.monotonic()
        if now - self.map_checked > 5.0:
            self.map_checked = now
            self.map_name = (running_map() if running_map else None) or self.map_param or None
        return self.map_name

    def _tick(self):
        mp = self._current_map()
        if self.url and mp and not self.busy and time.monotonic() >= self.backoff:
            try:
                t = self.tf.lookup_transform(self.map_frame, self.base_frame, Time())
            except Exception:       # nog geen lokalisatie (geen map -> base)
                t = None
            if t:
                plan = self.plan if self.plan and time.monotonic() - self.plan[0] < 3.0 else None
                body = {"robot": self.robot, "nr": self.nr, "map": mp,
                        "x": t.transform.translation.x, "y": t.transform.translation.y,
                        "yaw": yaw_of(t.transform.rotation), "v": self.v, "loc_ok": self.loc_ok,
                        "goal": plan[1][-1] if plan else None, "path": plan[1] if plan else []}
                self.busy = True
                threading.Thread(target=self._post, args=(body,), daemon=True).start()
        self._publish()

    def _post(self, body):
        try:
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as r:
                reply = json.loads(r.read())
            self.others, self.others_t = reply.get("robots", []), time.monotonic()
        except Exception:
            self.backoff = time.monotonic() + 5.0       # monitor weg: rustig opnieuw
        finally:
            self.busy = False

    # --- de andere robots ---------------------------------------------------
    def _publish(self):
        fresh = time.monotonic() - self.others_t < 2.0
        others = [r for r in self.others if fresh and r.get("age", 99) < 3.0]
        stamp = self.get_clock().now().to_msg()
        hdr = Header(frame_id=self.map_frame, stamp=stamp)
        ma = MarkerArray(markers=[Marker(header=hdr, action=Marker.DELETEALL)])
        pa = PoseArray(header=hdr)
        ring = []
        for i, r in enumerate(others):
            x, y, yaw = r["x"], r["y"], r["yaw"]
            pose = Pose()
            pose.position.x, pose.position.y = x, y
            pose.orientation.z, pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
            pa.poses.append(pose)
            life = Duration(seconds=1.0).to_msg()
            body = Marker(header=hdr, ns="fleet", id=3 * i, type=Marker.CYLINDER, pose=pose, lifetime=life,
                          color=color(r.get("nr"), 0.85))
            body.scale.x = body.scale.y = 2 * self.radius
            body.scale.z = 0.2
            body.pose.position.z = 0.1
            arrow = Marker(header=hdr, ns="fleet", id=3 * i + 1, type=Marker.ARROW, pose=pose, lifetime=life,
                           color=color(r.get("nr")))
            arrow.scale.x, arrow.scale.y, arrow.scale.z = 0.35, 0.05, 0.05
            text = Marker(header=hdr, ns="fleet", id=3 * i + 2, type=Marker.TEXT_VIEW_FACING, lifetime=life,
                          text=str(r.get("nr") or r["robot"]), color=ColorRGBA(r=1.0, g=1.0, b=1.0, a=1.0))
            text.pose.position.x, text.pose.position.y, text.pose.position.z = x, y, 0.4
            text.scale.z = 0.18
            ma.markers += [body, arrow, text]
            ring += [(x + self.radius * math.cos(a), y + self.radius * math.sin(a), 0.1)
                     for a in (k * math.pi / 8 for k in range(16))]
        self.pub_markers.publish(ma)
        self.pub_poses.publish(pa)
        self.pub_cloud.publish(point_cloud2.create_cloud_xyz32(hdr, ring))


def main():
    rclpy.init()
    node = FleetNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
