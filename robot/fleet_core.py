"""fleet_core - de logica van fleet_node zonder ROS (los te testen).

* FleetMemory: onthoudt de laatst gekende positie van elke andere robot, ook
  als de monitor wegvalt. Hoe ouder, hoe onzekerder: een robot kan in `age`
  seconden hoogstens v_max * age verder zijn, dus de cirkel waar hij kan zijn
  groeit. Na `forget_s` is hij weg.
* nearby(): welke robots zijn binnen een straal, met afstand en richting
  t.o.v. de eigen rijrichting.
* keepout_mask(): OccupancyGrid-data (0..100) rond de eigen robot voor de
  Nav2 KeepoutFilter: per andere robot een volle schijf (zijn lichaam) met
  een aflopende rand. Elke update wordt de mask volledig opnieuw getekend,
  dus oude posities blijven niet hangen (het probleem van een obstacle layer).
"""
import math

V_MAX = 0.22          # TurtleBot3 Burger, m/s


class FleetMemory:
    def __init__(self, stale_s=3.0, forget_s=15.0, max_grow=1.0, v_max=V_MAX):
        self.stale_s, self.forget_s, self.max_grow, self.v_max = stale_s, forget_s, max_grow, v_max
        self.robots = {}          # name -> dict(x, y, yaw, v, nr, seen)

    def update(self, reports, now):
        """reports: lijst uit het antwoord van de monitor (met 'age' in s)."""
        for r in reports:
            seen = now - float(r.get("age", 0))
            old = self.robots.get(r["robot"])
            if old is None or seen >= old["seen"]:
                self.robots[r["robot"]] = {"robot": r["robot"], "nr": r.get("nr"), "x": float(r["x"]),
                                           "y": float(r["y"]), "yaw": float(r.get("yaw", 0)),
                                           "v": float(r.get("v", 0)), "seen": seen}

    def get(self, now):
        """Alle onthouden robots met age, stale en grow (extra onzekerheid in m)."""
        out = []
        for name in list(self.robots):
            r = self.robots[name]
            age = now - r["seen"]
            if age > self.forget_s:
                del self.robots[name]
                continue
            grow = 0.0 if age <= self.stale_s else min(self.max_grow, self.v_max * (age - self.stale_s))
            out.append(dict(r, age=age, stale=age > self.stale_s, grow=grow))
        return out


def nearby(own, others, radius):
    """own = (x, y, yaw). Robots binnen radius (m), dichtste eerst, met
    afstand en hoek t.o.v. de eigen neus (+ = links, - = rechts)."""
    x0, y0, yaw0 = own
    out = []
    for r in others:
        d = math.hypot(r["x"] - x0, r["y"] - y0)
        if d <= radius:
            b = math.atan2(r["y"] - y0, r["x"] - x0) - yaw0
            out.append(dict(r, dist=d, bearing=math.atan2(math.sin(b), math.cos(b))))
    return sorted(out, key=lambda r: r["dist"])


def describe(r):
    deg = math.degrees(r["bearing"])
    side = ("voor" if abs(deg) < 30 else "achter" if abs(deg) > 150 else
            ("links" if deg > 0 else "rechts") + ("-voor" if abs(deg) < 75 else "-achter" if abs(deg) > 105 else ""))
    old = " (laatst gezien %d s geleden)" % r["age"] if r.get("stale") else ""
    return "%s: %.2f m %s%s" % (r.get("nr") or r["robot"], r["dist"], side, old)


def keepout_mask(own, others, robot_radius=0.14, margin=0.25, size=6.0, res=0.05):
    """Mask van size x size m rond de eigen robot (map-frame, niet gedraaid).

    Waarden (OccupancyGrid 0..100, KeepoutFilter met base 0, multiplier 1):
      lichaam van de andere robot (+ onzekerheid als hij oud is) -> 100
      rand van `margin` m daarbuiten -> aflopend van 90 naar 10
    Een oude positie krijgt geen 100 maar hoogstens 70: Nav2 vermijdt de plek,
    maar plant er nog door als het niet anders kan.
    Staat de eigen robot al in de schijf van een andere (te dicht), dan wordt
    die schijf afgetopt op 70, anders zit hij "in een obstakel" en kan hij
    niet meer wegrijden.
    Geeft (origin_x, origin_y, width, height, data) terug; data rij per rij,
    rij 0 = onderaan (OccupancyGrid-conventie)."""
    n = int(round(size / res))
    ox = math.floor((own[0] - size / 2) / res) * res     # op het raster, anders verspringt de mask
    oy = math.floor((own[1] - size / 2) / res) * res
    data = [0] * (n * n)
    for r in others:
        core = robot_radius + r.get("grow", 0.0)
        outer = core + margin
        cap = 70 if r.get("stale") else 100
        if math.hypot(r["x"] - own[0], r["y"] - own[1]) < core + robot_radius:
            cap = min(cap, 70)
        i0, i1 = int((r["x"] - outer - ox) / res), int((r["x"] + outer - ox) / res) + 1
        j0, j1 = int((r["y"] - outer - oy) / res), int((r["y"] + outer - oy) / res) + 1
        for j in range(max(0, j0), min(n, j1)):
            cy = oy + (j + 0.5) * res
            for i in range(max(0, i0), min(n, i1)):
                d = math.hypot(ox + (i + 0.5) * res - r["x"], cy - r["y"])
                if d <= core:
                    v = cap
                elif d <= outer:
                    v = int(round(min(cap - 10, 90) - 80 * (d - core) / margin))
                else:
                    continue
                k = j * n + i
                if v > data[k]:
                    data[k] = v
    return ox, oy, n, n, data
