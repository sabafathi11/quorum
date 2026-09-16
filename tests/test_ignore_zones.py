"""Exact-pixel semantics for ignore zones."""
import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("ignore_zones_plugin", ROOT / "plugins/ignore_zones/plugin.py")
Z = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = Z
spec.loader.exec_module(Z)


def payload(mask, left=0, top=0):
    return {"box": [left, top, mask.shape[1], mask.shape[0]], "rle": Z.CODEC.encode_rle(mask)}


def test_only_a_fully_contained_mask_is_ignored():
    zone = np.zeros((6, 6), dtype=bool); zone[1:5, 1:5] = True
    inside = np.ones((2, 2), dtype=bool)
    crossing = np.ones((2, 2), dtype=bool)
    assert Z.contains(payload(zone), payload(inside, 2, 2))
    assert not Z.contains(payload(zone), payload(crossing, 4, 4))


def test_zone_keyframes_hold_per_camera_without_replacing_other_cameras():
    zones = {"cam1": {"5": {"name": "first"}, "20": {"name": "later"}},
             "cam2": {"7": {"name": "other camera"}}}
    assert Z.zone_from_state(zones, "cam1", 6)["name"] == "first"
    assert Z.zone_from_state(zones, "cam1", 20)["name"] == "later"
    assert Z.zone_from_state(zones, "cam2", 99)["name"] == "other camera"


if __name__ == "__main__":
    test_only_a_fully_contained_mask_is_ignored()
    print("ignore-zone containment passed")
