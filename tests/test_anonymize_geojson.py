"""devtools/anonymize_geojson.py: move a map elsewhere, keep its shape and bearings."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from custom_components.groundskeeper.zone_bearing import zone_bearing

_PATH = Path(__file__).parents[1] / "devtools" / "anonymize_geojson.py"
_spec = importlib.util.spec_from_file_location("anonymize_geojson", _PATH)
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)

# A tilted rectangle ~40 m x 15 m, a notch obstacle, and a planned path.
LAT, LON = 40.0, -80.0
RING = [
    [LON, LAT],
    [LON + 0.00040, LAT + 0.00010],
    [LON + 0.00046, LAT + 0.00022],
    [LON + 0.00006, LAT + 0.00012],
    [LON, LAT],
]
SAMPLE = {
    "type": "FeatureCollection",
    "name": "Lawn Areas",
    "bbox": [LON, LAT, LON + 0.0005, LAT + 0.0003],
    "features": [
        {
            "type": "Feature",
            "properties": {"title": "Lawn", "hash": 111222333444, "device_name": "Luba-XYZ"},
            "geometry": {"type": "Polygon", "coordinates": [RING]},
        },
        {
            "type": "Feature",
            "properties": {"title": "Path 1", "hash": 555, "parent_hash_a": 111222333444},
            "geometry": {"type": "LineString", "coordinates": [[LON, LAT, 212.5], [LON + 0.0002, LAT + 0.0001, 212.9]]},
        },
    ],
}


def _points(doc):
    return [p for g in ag._geometries(doc) for p in ag._points(g["coordinates"])]


def _local_metres(points):
    lat0 = sum(p[1] for p in points) / len(points)
    kx = 111320.0 * math.cos(math.radians(lat0))
    return [(p[0] * kx, p[1] * 110540.0) for p in points]


def _km_between(a, b):
    mean_lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111.32 * math.cos(mean_lat), (a[1] - b[1]) * 110.54)


def test_translation_only_keeps_shape_and_bearings():
    out, _ = ag.anonymize(SAMPLE, origin=(-33.0, 151.0))
    before, after = _points(SAMPLE), _points(out)
    assert len(before) == len(after)
    a, b = _local_metres(before), _local_metres(after)
    for i in range(1, len(a)):
        assert (b[i][0] - b[0][0]) == pytest.approx(a[i][0] - a[0][0], abs=0.03)
        assert (b[i][1] - b[0][1]) == pytest.approx(a[i][1] - a[0][1], abs=0.03)


def test_zone_bearing_is_unchanged_after_the_move():
    out, _ = ag.anonymize(SAMPLE, origin=(-33.0, 151.0))
    before = zone_bearing(SAMPLE["features"][0]["geometry"]["coordinates"][0])
    after = zone_bearing(out["features"][0]["geometry"]["coordinates"][0])
    assert after.bearing == pytest.approx(before.bearing, abs=0.2)
    assert after.aspect == pytest.approx(before.aspect, rel=0.01)
    assert after.fill == pytest.approx(before.fill, abs=0.01)


def test_output_lands_at_the_requested_origin_far_from_the_input():
    out, origin = ag.anonymize(SAMPLE, origin=(-33.0, 151.0))
    pts = _points(out)
    centre = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    assert _km_between(centre, (151.0, -33.0)) < 0.1
    assert origin == (-33.0, 151.0)
    assert _km_between(centre, (LON, LAT)) > 1000


def test_default_destination_is_random_and_unrelated_to_the_input():
    first = ag.anonymize(SAMPLE)[1]
    second = ag.anonymize(SAMPLE)[1]
    assert first != second
    assert _km_between((first[1], first[0]), (LON, LAT)) > 0  # never claims to be the input


def test_hashes_are_replaced_and_references_stay_consistent():
    out, _ = ag.anonymize(SAMPLE, origin=(0.0, 0.0))
    lawn, path = (f["properties"] for f in out["features"])
    assert lawn["hash"] != 111222333444
    assert path["parent_hash_a"] == lawn["hash"]
    assert path["hash"] not in (555, lawn["hash"])
    assert "111222333444" not in json.dumps(out)


def test_bbox_altitude_and_device_keys_are_dropped():
    out, _ = ag.anonymize(SAMPLE, origin=(0.0, 0.0))
    text = json.dumps(out)
    assert "bbox" not in out
    assert "device_name" not in text and "Luba-XYZ" not in text
    assert all(len(p) == 2 for p in _points(out))


def test_input_is_not_modified():
    snapshot = json.dumps(SAMPLE, sort_keys=True)
    ag.anonymize(SAMPLE, origin=(10.0, 10.0))
    assert json.dumps(SAMPLE, sort_keys=True) == snapshot


def test_rejects_a_document_without_coordinates():
    with pytest.raises(ValueError):
        ag.anonymize({"type": "FeatureCollection", "features": []})


def test_cli_is_reproducible_with_an_explicit_origin(tmp_path, capsys):
    src = tmp_path / "map.geojson"
    src.write_text(json.dumps(SAMPLE))
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert ag.main([str(src), "-o", str(a), "--origin", "12.5,-45.25"]) == 0
    assert ag.main([str(src), "-o", str(b), "--origin", "12.5,-45.25"]) == 0
    assert a.read_text() == b.read_text()
    assert "moved to 12.5000, -45.2500" in capsys.readouterr().err
