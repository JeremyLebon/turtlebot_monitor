"""python3 robot/test_fleet_core.py - test de vlootlogica zonder ROS."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fleet_core import FleetMemory, describe, keepout_mask, nearby  # noqa: E402


def test_memory_keeps_and_grows_and_forgets():
    m = FleetMemory(stale_s=3, forget_s=15, max_grow=1.0, v_max=0.2)
    m.update([{"robot": "tb2", "nr": "2", "x": 1, "y": 2, "age": 0.5}], now=100)
    r = m.get(101)[0]
    assert not r["stale"] and r["grow"] == 0 and abs(r["age"] - 1.5) < 1e-9
    r = m.get(108.5)[0]                     # monitor weg: 8,5 s oud -> 0,2*5,5 = 1,1, afgetopt op 1,0
    assert r["stale"] and r["grow"] == 1.0
    assert abs(m.get(105.5)[0]["grow"] - 0.2 * 3) < 1e-9   # 6 s oud, 3 s boven stale
    assert m.get(116) == []                 # vergeten


def test_memory_ignores_older_report():
    m = FleetMemory()
    m.update([{"robot": "tb2", "x": 5, "y": 5, "age": 0}], now=10)
    m.update([{"robot": "tb2", "x": 1, "y": 1, "age": 4}], now=11)   # ouder dan wat we al weten
    assert m.get(11)[0]["x"] == 5


def test_nearby_order_and_side():
    others = [{"robot": "a", "nr": "3", "x": 1.0, "y": 0.0, "age": 0},
              {"robot": "b", "nr": "4", "x": 0.0, "y": 0.5, "age": 0},
              {"robot": "c", "nr": "5", "x": 5.0, "y": 5.0, "age": 0}]
    near = nearby((0, 0, 0), others, 2.0)
    assert [r["robot"] for r in near] == ["b", "a"]
    assert describe(near[0]).startswith("4: 0.50 m links")
    assert describe(near[1]) == "3: 1.00 m voor"
    near = nearby((0, 0, math.pi), others, 2.0)              # omgedraaid
    assert describe(near[1]) == "3: 1.00 m achter"


def _at(mask, x, y):
    ox, oy, w, h, data = mask
    return data[int((y - oy) / 0.05) * w + int((x - ox) / 0.05)]


def test_mask_disc_and_gradient():
    other = [{"robot": "a", "x": 1.0, "y": 0.0, "grow": 0, "stale": False}]
    mask = keepout_mask((0, 0, 0), other)
    assert _at(mask, 1.0, 0.0) == 100              # lichaam
    assert _at(mask, 1.0, 0.30) == 100             # beide stralen + marge: midden eigen robot mag hier niet
    assert 10 <= _at(mask, 1.0, 0.45) <= 90        # rand (zoals de inflation layer)
    assert _at(mask, 1.0, 0.9) == 0                # vrij
    assert _at(mask, 0.0, 0.0) == 0                # eigen plek vrij


def test_mask_touching_distance_is_lethal():
    # twee Burgers raken elkaar bij ~0,21 m tussen de middelpunten: dat moet dodelijk zijn
    other = [{"robot": "a", "x": 1.0, "y": 0.0, "grow": 0, "stale": False}]
    mask = keepout_mask((0, 0, 0), other)
    for a in range(0, 360, 30):
        x, y = 1.0 + 0.24 * math.cos(math.radians(a)), 0.24 * math.sin(math.radians(a))
        assert _at(mask, x, y) == 100, a


def test_mask_leads_in_driving_direction():
    other = [{"robot": "a", "x": 1.0, "y": 0.0, "yaw": 0.0, "v": 0.22, "grow": 0, "stale": False}]
    mask = keepout_mask((0, 0, 0), other)
    assert _at(mask, 1.0 + 0.33 + 0.10, 0.0) == 100    # vooruit verlengd (0,22 * 0,6 = 0,13 m)
    assert _at(mask, 1.0 - 0.33 - 0.10, 0.0) < 100     # achteruit niet


def test_mask_stale_is_soft_and_bigger():
    other = [{"robot": "a", "x": 1.5, "y": 0.0, "grow": 0.5, "stale": True, "v": 0.2}]
    mask = keepout_mask((0, 0, 0), other)
    assert _at(mask, 1.5, 0.0) == 70
    assert _at(mask, 1.5, 0.7) == 70               # onzekerheid groeit mee


def test_mask_too_close_stays_lethal():
    # te dicht: blijft 100, Nav2 maakt er 252 van zodat de robot weg kan (override_lethal_cost)
    other = [{"robot": "a", "x": 0.2, "y": 0.0, "grow": 0, "stale": False}]
    assert _at(keepout_mask((0, 0, 0), other), 0.2, 0.0) == 100


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
