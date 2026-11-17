#!/usr/bin/env python3
"""Nep-vloot voor de vlootkaart: test de monitor zonder robots.

Zet een demokaart "demo_vloot" (klaslokaal 12 x 8 m met tafels) in de
kaartenbibliotheek en laat N robots er rondjes op rijden. Elke robot stuurt
4x/s zijn positie naar POST /fleet, zoals fleet_node op een echte robot.

    python3 tools/fake_fleet.py                       # monitor op localhost:8090, 5 robots
    python3 tools/fake_fleet.py --url http://10.0.0.5:8090 --robots 9

Enkel de Python-standaardbibliotheek.
"""
import argparse
import base64
import json
import math
import threading
import time
import urllib.request

RES = 0.05
W_M, H_M = 12.0, 8.0
MAP = "demo_vloot"


def demo_map():
    w, h = int(W_M / RES), int(H_M / RES)
    px = bytearray([205]) * (w * h)          # onbekend

    def rect(x0, y0, x1, y1, v):             # meter, oorsprong linksonder
        for y in range(int(y0 / RES), int(y1 / RES)):
            for x in range(int(x0 / RES), int(x1 / RES)):
                if 0 <= x < w and 0 <= y < h:
                    px[(h - 1 - y) * w + x] = v
    rect(0.3, 0.3, 11.7, 7.7, 0)             # muren
    rect(0.4, 0.4, 11.6, 7.6, 254)           # vrije vloer
    rect(5.0, 7.5, 7.0, 7.7, 254)            # deur
    for row in (2.0, 4.2):                    # twee rijen tafels
        for col in (1.6, 4.4, 7.2):
            rect(col, row, col + 2.2, row + 0.8, 0)
    rect(9.9, 5.6, 11.6, 7.6, 0)             # kast / bureau docent
    pgm = b"P5\n# demo_vloot\n%d %d\n255\n" % (w, h) + bytes(px)
    yaml = ("image: %s.pgm\nmode: trinary\nresolution: %.3f\norigin: [-0.5, -0.5, 0.0]\n"
            "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.25\n" % (MAP, RES))
    return {"format": "turtlebot-map/1", "name": MAP, "created": time.time(), "from": "fake_fleet",
            "yaml": yaml, "pgm_b64": base64.b64encode(pgm).decode(), "zones": [], "routes": {}}


# rondjes in de gangen tussen de tafels (map-frame = kaart + origin -0.5)
O = -0.5
LOOPS = [
    [(1.0, 1.2), (10.8, 1.2), (10.8, 3.5), (1.0, 3.5)],
    [(10.8, 3.5), (1.0, 3.5), (1.0, 1.2), (10.8, 1.2)],
    [(1.0, 5.5), (9.0, 5.5), (9.0, 6.9), (1.0, 6.9)],
    [(4.0, 1.2), (4.0, 3.5), (4.0, 6.9), (6.9, 6.9), (6.9, 1.2)],
    [(9.4, 1.2), (9.4, 5.0), (6.9, 5.0), (6.9, 1.2)],
    [(1.0, 6.9), (1.0, 1.2), (3.9, 1.2), (3.9, 6.9)],
    [(6.0, 6.9), (6.0, 3.6), (3.0, 3.6), (3.0, 6.9)],
    [(10.8, 1.2), (10.8, 4.8), (9.6, 4.8), (9.6, 1.2)],
    [(2.0, 5.4), (8.0, 5.4), (8.0, 3.6), (2.0, 3.6)],
]


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.read()


def robot(url, nr, speed, stop):
    loop = [(x + O, y + O) for x, y in LOOPS[(nr - 1) % len(LOOPS)]]
    x, y = loop[0]
    i, yaw, pause = 1, 0.0, 0.0
    dt = 0.25
    while not stop.is_set():
        gx, gy = loop[i]
        d = math.hypot(gx - x, gy - y)
        v = 0.0
        if pause > 0:
            pause -= dt
        elif d < 0.05:
            i, pause = (i + 1) % len(loop), 1.0        # even stilstaan op het doel
        else:
            want = math.atan2(gy - y, gx - x)
            err = (want - yaw + math.pi) % (2 * math.pi) - math.pi
            yaw += max(-1.5 * dt, min(1.5 * dt, err))
            yaw = math.atan2(math.sin(yaw), math.cos(yaw))
            if abs(err) < 0.3:
                v = min(speed, d / dt)
                x += v * dt * math.cos(yaw)
                y += v * dt * math.sin(yaw)
        path = [(x + (gx - x) * k / 10, y + (gy - y) * k / 10) for k in range(1, 11)]
        try:
            post(url + "/fleet", {"robot": "turtlebot%d" % nr, "nr": str(nr), "map": MAP,
                                  "x": x, "y": y, "yaw": yaw, "v": v, "loc_ok": True,
                                  "goal": [gx, gy], "path": path})
        except OSError as exc:
            print("robot %d: %s" % (nr, exc))
            time.sleep(2)
        time.sleep(dt)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8090")
    ap.add_argument("--robots", type=int, default=5)
    ap.add_argument("--speed", type=float, default=0.22, help="m/s (TurtleBot3 Burger max 0.22)")
    a = ap.parse_args()
    post(a.url + "/maps/" + MAP, demo_map())
    print("kaart %s in de bibliotheek; %d robots rijden - Ctrl+C stopt" % (MAP, a.robots))
    stop = threading.Event()
    for nr in range(1, a.robots + 1):
        threading.Thread(target=robot, args=(a.url, nr, a.speed, stop), daemon=True).start()
        time.sleep(0.4)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
