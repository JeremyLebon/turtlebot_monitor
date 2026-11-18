#!/usr/bin/env python3
"""fleet_node - de robot in de vloot (turtlebot_monitor docs/vloot.md).

launch_control_node start hem samen met Navigatie (log /tmp/launch_fleet.log)
en stopt hem mee; enkel met een monitor (MONITOR_URL), niet als
/root/turtlebot_state/fleet_off bestaat. Draait naast Navigatie. Zolang Navigatie loopt met een kaart (dezelfde kaart uit
de kaartenbibliotheek op alle robots = hetzelfde map-frame):

  * stuurt hij 4x/s zijn positie (TF map -> base_footprint), snelheid, doel en
    gepland pad naar de monitor: POST <monitor>/fleet;
  * krijgt hij in het antwoord de andere robots op dezelfde kaart terug en
    ONTHOUDT ze (fleet_core.FleetMemory): valt de monitor weg, dan blijven de
    laatst gekende posities staan, met een groeiende onzekerheid, tot 15 s;
  * publiceert:
      /fleet/robots        visualization_msgs/MarkerArray  (RViz, Lichtblick; grijs = oud)
      /fleet/poses         geometry_msgs/PoseArray
      /fleet/keepout_mask  nav_msgs/OccupancyGrid (6 x 6 m rond de robot)
      /fleet/filter_info   nav2_msgs/CostmapFilterInfo -> Nav2 KeepoutFilter:
                           de andere robots als obstakel in de costmap
  * service /fleet/nearby (std_srvs/Trigger): "wie is er in de buurt?"
        ros2 service call /fleet/nearby std_srvs/srv/Trigger
        -> "2 robot(s) binnen 2.0 m: 4: 0.62 m links-voor; 7: 1.80 m achter"

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
from nav2_msgs.msg import CostmapFilterInfo
from nav_msgs.msg import OccupancyGrid, Odometry, Path
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from std_msgs.msg import ColorRGBA, Header
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformListener
from visualization_msgs.msg import Marker, MarkerArray

from fleet_core import FleetMemory, describe, keepout_mask, nearby

try:                                   # zelfde map als map_bundle.py op de robot
    from map_bundle import running_map
except ImportError:                    # concept los getest: kaartnaam via parameter
    running_map = None

# zelfde kleuren als de vlootkaart op de monitor (index = nr)
COLORS = ["#58a6ff", "#3fb950", "#e3b341", "#f778ba", "#a371f7", "#ff7b38", "#39c5cf", "#d2a8ff", "#7ee787", "#ffa198"]
LATCHED = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)


def color(nr, a=1.0, grey=False):
    if grey:
        return ColorRGBA(r=0.45, g=0.45, b=0.45, a=a)
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
        p = self.declare_parameter
        self.map_frame = p("map_frame", "map").value
        self.base_frame = p("base_frame", "base_footprint").value
        self.map_param = p("map_name", "").value
        self.radius = p("robot_radius", 0.14).value           # Burger ~0.14 m
        self.margin = p("keepout_margin", 0.4).value          # aflopende rand rond een andere robot
        self.safety = p("keepout_safety", 0.05).value         # extra afstand tussen de twee robots
        self.lead_s = p("keepout_lead_s", 0.6).value          # schijf vooruit in de rijrichting: v * lead_s
        self.near_r = p("nearby_radius", 2.0).value
        self.memory = FleetMemory(stale_s=p("stale_s", 3.0).value, forget_s=p("forget_s", 15.0).value,
                                  max_grow=p("max_grow", 1.0).value)
        rate = p("rate", 4.0).value

        self.tf = Buffer()
        TransformListener(self.tf, self)
        self.own = None                 # (x, y, yaw) in map
        self.v = 0.0
        self.plan = None                # (time, [[x, y], ...])
        self.loc_ok = True
        self.map_name, self.map_checked = None, 0.0
        self.busy, self.backoff = False, 0.0
        self.lock = threading.Lock()    # memory: HTTP-thread schrijft, timers lezen

        self.create_subscription(Odometry, "/odom", lambda m: setattr(self, "v", m.twist.twist.linear.x), 10)
        self.create_subscription(Path, "/plan", self._on_plan, 1)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._on_amcl, LATCHED)
        self.pub_markers = self.create_publisher(MarkerArray, "/fleet/robots", 1)
        self.pub_poses = self.create_publisher(PoseArray, "/fleet/poses", 1)
        self.pub_mask = self.create_publisher(OccupancyGrid, "/fleet/keepout_mask", LATCHED)
        # filter info and mask only once the robot is localized (map -> base for
        # LOCALIZED_S): earlier the KeepoutFilter of the local costmap (odom)
        # asked map -> odom before AMCL published it and logged an ERROR
        # (turtlebot05, 1.10.0). Lost localization later: keep the filter on.
        self.pub_info = self.create_publisher(CostmapFilterInfo, "/fleet/filter_info", LATCHED)
        self.localized_s = p("localized_s", 2.0).value
        self.tf_since = None            # monotonic time since map -> base is found
        self.filter_on = False
        self.create_service(Trigger, "/fleet/nearby", self._on_nearby)
        self.create_timer(1.0 / rate, self._tick)
        self.create_timer(0.5, self._publish_mask)     # 2 Hz: de KeepoutFilter logt elke nieuwe mask
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

    def _others(self):
        with self.lock:
            return self.memory.get(time.monotonic())

    def _tick(self):
        try:
            t = self.tf.lookup_transform(self.map_frame, self.base_frame, Time())
            self.own = (t.transform.translation.x, t.transform.translation.y, yaw_of(t.transform.rotation))
            if self.tf_since is None:
                self.tf_since = time.monotonic()
        except Exception:               # nog geen lokalisatie (geen map -> base)
            self.own = None
            self.tf_since = None
        mp = self._current_map()
        if self.url and mp and self.own and not self.busy and time.monotonic() >= self.backoff:
            plan = self.plan if self.plan and time.monotonic() - self.plan[0] < 3.0 else None
            body = {"robot": self.robot, "nr": self.nr, "map": mp,
                    "x": self.own[0], "y": self.own[1], "yaw": self.own[2], "v": self.v, "loc_ok": self.loc_ok,
                    "goal": plan[1][-1] if plan else None, "path": plan[1] if plan else []}
            self.busy = True
            threading.Thread(target=self._post, args=(body,), daemon=True).start()
        self._publish_markers()

    def _post(self, body):
        try:
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as r:
                reply = json.loads(r.read())
            with self.lock:
                self.memory.update(reply.get("robots", []), time.monotonic())
        except Exception:
            self.backoff = time.monotonic() + 2.0       # monitor weg: geheugen houdt de rest
        finally:
            self.busy = False

    # --- de andere robots ---------------------------------------------------
    def _on_nearby(self, _req, resp):
        if not self.own:
            resp.success, resp.message = False, "eigen positie onbekend (Navigatie/lokalisatie?)"
            return resp
        near = nearby(self.own, self._others(), self.near_r)
        resp.success = True
        resp.message = ("%d robot(s) binnen %.1f m: %s" % (len(near), self.near_r, "; ".join(map(describe, near)))
                        if near else "geen robots binnen %.1f m" % self.near_r)
        return resp

    def _publish_mask(self):
        if not self.own:
            return
        info = None
        if not self.filter_on:
            if time.monotonic() - self.tf_since < self.localized_s:
                return
            info = CostmapFilterInfo(type=0, filter_mask_topic="/fleet/keepout_mask", base=0.0, multiplier=1.0)
            info.header.frame_id = self.map_frame      # type 0 = KEEPOUT
            self.filter_on = True
        ox, oy, w, h, data = keepout_mask(self.own, self._others(), self.radius, self.margin,
                                       self.safety, self.lead_s)
        g = OccupancyGrid()
        g.header = Header(frame_id=self.map_frame, stamp=self.get_clock().now().to_msg())
        g.info.resolution, g.info.width, g.info.height = 0.05, w, h
        g.info.origin.position.x, g.info.origin.position.y = ox, oy
        g.info.origin.orientation.w = 1.0
        g.data = data
        self.pub_mask.publish(g)
        if info:                        # first mask: now switch the filter on
            self.pub_info.publish(info)
            self.get_logger().info("gelokaliseerd: andere robots in de costmap (fleet_filter)")

    def _publish_markers(self):
        stamp = self.get_clock().now().to_msg()
        hdr = Header(frame_id=self.map_frame, stamp=stamp)
        ma = MarkerArray(markers=[Marker(header=hdr, action=Marker.DELETEALL)])
        pa = PoseArray(header=hdr)
        for i, r in enumerate(self._others()):
            x, y, yaw, old = r["x"], r["y"], r["yaw"], r["stale"]
            pose = Pose()
            pose.position.x, pose.position.y = x, y
            pose.orientation.z, pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
            pa.poses.append(pose)
            body = Marker(header=hdr, ns="fleet", id=3 * i, type=Marker.CYLINDER, pose=pose,
                          color=color(r.get("nr"), 0.4 if old else 0.85, grey=old))
            body.scale.x = body.scale.y = 2 * (self.radius + r["grow"])     # oud: cirkel waar hij kan zijn
            body.scale.z = 0.05 if old else 0.2
            arrow = Marker(header=hdr, ns="fleet", id=3 * i + 1, type=Marker.ARROW, pose=pose,
                           color=color(r.get("nr"), grey=old))
            arrow.scale.x, arrow.scale.y, arrow.scale.z = 0.35, 0.05, 0.05
            label = str(r.get("nr") or r["robot"]) + (" (%d s)" % r["age"] if old else "")
            text = Marker(header=hdr, ns="fleet", id=3 * i + 2, type=Marker.TEXT_VIEW_FACING, text=label,
                          color=ColorRGBA(r=1.0, g=1.0, b=1.0, a=1.0))
            text.pose.position.x, text.pose.position.y, text.pose.position.z = x, y, 0.4
            text.scale.z = 0.18
            ma.markers += [body, arrow, text]
        self.pub_markers.publish(ma)
        self.pub_poses.publish(pa)


def main():
    rclpy.init()
    node = FleetNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):   # SIGINT/SIGTERM (Navigatie stoppen)
        pass
    finally:
        node.destroy_node()
        try:
            rclpy.try_shutdown()
        except Exception:               # SIGINT/SIGTERM: the signal handler already shut down rclpy
            pass


if __name__ == "__main__":
    main()
