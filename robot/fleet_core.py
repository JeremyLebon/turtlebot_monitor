"""fleet_core - de logica van fleet_node zonder ROS (los te testen).

* FleetMemory: onthoudt de laatst gekende positie van elke andere robot, ook
  als de monitor wegvalt. Hoe ouder, hoe onzekerder: een robot kan in `age`
  seconden hoogstens v_max * age verder zijn, dus de cirkel waar hij kan zijn
  groeit. Na `forget_s` is hij weg.
* nearby(): welke robots zijn binnen een straal, met afstand en richting
  t.o.v. de eigen rijrichting.
* keepout_mask(): OccupancyGrid-data (0..100) rond de eigen robot voor de
  Nav2 KeepoutFilter: per andere robot een dodelijke schijf (beide stralen +
  marge, verlengd in zijn rijrichting) met een rand zoals de inflation layer. Elke update wordt de mask volledig opnieuw getekend,
  dus oude posities blijven niet hangen (het probleem van een obstacle layer).
* amcl_ellipse(): onzekerheid van AMCL als ellips, voor de vlootkaart.
* costmap_to_map(): de lokale costmap (frame odom) als klein raster in het
  map-frame, voor de vlootkaart (enkel als de monitor erom vraagt).
"""
import base64
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


def _seg_dist(px, py, ax, ay, bx, by):
    """Afstand van punt p tot het lijnstuk a-b."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def keepout_mask(own, others, robot_radius=0.14, margin=0.4, safety=0.05, lead_s=0.6,
                 size=6.0, res=0.05, scaling=5.0):
    """Mask van size x size m rond de eigen robot (map-frame, niet gedraaid).

    Costmap-filters komen na de lagen: de inflation layer ziet deze mask niet
    (zoals bij de verboden zones, 1.9.13). Dus de mask bevat zelf alles:
      100 (dodelijk): waar het MIDDEN van de eigen robot niet mag komen, dus
          zijn straal + die van de andere + `safety` rond de andere robot,
          verlengd in zijn rijrichting over v * `lead_s` (de positie is tot
          ~0,5 s oud: via de monitor 4x/s, mask 2x/s);
      rand van `margin` m daarbuiten: 99 * exp(-scaling * d), zoals de
          inflation layer van TurtleBot3 (kost 251 en lager), zodat Nav2 er
          met een boog omheen plant.
    Een oude positie (stale) is hoogstens 70: Nav2 vermijdt de plek, maar plant
    er nog door als het niet anders kan; de onzekerheid (grow) komt erbij.
    Staat de eigen robot toch in het dodelijke deel (te dicht), dan maakt
    Nav2 er 252 van (override_lethal_cost in de fleet_filter) en kan hij weg.
    Geeft (origin_x, origin_y, width, height, data) terug; data rij per rij,
    rij 0 = onderaan (OccupancyGrid-conventie)."""
    n = int(round(size / res))
    ox = math.floor((own[0] - size / 2) / res) * res     # op het raster, anders verspringt de mask
    oy = math.floor((own[1] - size / 2) / res) * res
    data = [0] * (n * n)
    for r in others:
        stale = bool(r.get("stale"))
        core = 2 * robot_radius + safety + r.get("grow", 0.0)
        lead = 0.0 if stale else (r.get("v") or 0.0) * lead_s
        ax, ay = r["x"], r["y"]
        bx, by = ax + lead * math.cos(r.get("yaw", 0.0)), ay + lead * math.sin(r.get("yaw", 0.0))
        outer = core + margin
        cap = 70 if stale else 100
        i0 = int((min(ax, bx) - outer - ox) / res)
        i1 = int((max(ax, bx) + outer - ox) / res) + 1
        j0 = int((min(ay, by) - outer - oy) / res)
        j1 = int((max(ay, by) + outer - oy) / res) + 1
        for j in range(max(0, j0), min(n, j1)):
            cy = oy + (j + 0.5) * res
            for i in range(max(0, i0), min(n, i1)):
                d = _seg_dist(ox + (i + 0.5) * res, cy, ax, ay, bx, by)
                if d <= core:
                    v = cap
                elif d <= outer:
                    v = int(round(min(cap - 1, 99) * math.exp(-scaling * (d - core))))
                    if v < 1:
                        continue
                else:
                    continue
                k = j * n + i
                if v > data[k]:
                    data[k] = v
    return ox, oy, n, n, data


def amcl_ellipse(cov):
    """AMCL-covariantie (6x6, rij per rij) -> {"a", "b", "ang", "syaw"}:
    standaardafwijkingen langs de assen van de ellips (m), hoek van de grote
    as (rad) en de onzekerheid van de richting (rad). Zelfde formule als
    nav_web_node.loc_from_amcl (Navigatie-pagina)."""
    cxx, cxy, cyy, cyaw = cov[0], cov[1], cov[7], cov[35]
    mid, half = (cxx + cyy) / 2, math.hypot((cxx - cyy) / 2, cxy)
    return {"a": round(math.sqrt(max(mid + half, 0.0)), 3), "b": round(math.sqrt(max(mid - half, 0.0)), 3),
            "ang": round(0.5 * math.atan2(2 * cxy, cxx - cyy), 3), "syaw": round(math.sqrt(max(cyaw, 0.0)), 3)}


def costmap_to_map(grid, frame_in_map, center, size=3.2, res=0.1):
    """Costmap (OccupancyGrid in zijn eigen frame, bv. odom) -> raster van
    size x size m rond `center` in het map-frame, cellen van `res` m.

    grid: {"w", "h", "res", "ox", "oy", "data"} (data 0..100, -1 = onbekend,
    rij 0 = onderaan); frame_in_map: (x, y, yaw) van dat frame in map.
    Per cel het maximum van 4 punten, zodat een dunne muur niet wegvalt.
    Geeft {"ox", "oy", "res", "w", "h", "b64"} terug: bytes 0..100,
    255 = onbekend, rij 0 = onderaan."""
    n = int(round(size / res))
    ox = math.floor((center[0] - size / 2) / res) * res
    oy = math.floor((center[1] - size / 2) / res) * res
    tx, ty, yaw = frame_in_map
    c, s = math.cos(yaw), math.sin(yaw)
    gw, gh, gr, gox, goy, data = grid["w"], grid["h"], grid["res"], grid["ox"], grid["oy"], grid["data"]
    q = res / 4
    out = bytearray(n * n)
    for j in range(n):
        for i in range(n):
            best = -1
            for dx, dy in ((-q, -q), (q, -q), (-q, q), (q, q)):
                px, py = ox + (i + 0.5) * res + dx - tx, oy + (j + 0.5) * res + dy - ty
                gi = int(math.floor((c * px + s * py - gox) / gr))     # map -> frame: R(-yaw)
                gj = int(math.floor((-s * px + c * py - goy) / gr))
                if 0 <= gi < gw and 0 <= gj < gh:
                    v = data[gj * gw + gi]
                    if v > best:
                        best = v
            out[j * n + i] = 255 if best < 0 else min(100, best)
    return {"ox": round(ox, 3), "oy": round(oy, 3), "res": res, "w": n, "h": n,
            "b64": base64.b64encode(bytes(out)).decode()}
