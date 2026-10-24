#!/usr/bin/env python3
"""Anonymize mower map GeoJSON before you share it (issues, bug reports, fixtures).

    python3 devtools/anonymize_geojson.py map.geojson -o map.anon.geojson

What it does:

* Moves every coordinate to a random place on Earth. The move is a pure
  translation: no rotation, no scaling. Cardinal bearings, distances, areas
  and shapes are unchanged, so Zone bearing, angle range and Path angle
  behaviour reproduce exactly.
* The destination is drawn from OS entropy and is not derived from the input,
  so the output does not reveal where the original was. Pass --origin to pick
  the destination yourself (for reproducible fixtures).
* Replaces device-specific `hash` / `parent_hash_*` ids with sequential fake
  ones, keeping the references between features consistent.
* Drops `bbox`, altitude values, and any property whose key looks like a
  device, account or network identifier.

What it does not do: the *shape and size* of your lawn are kept on purpose.
Someone who already suspects where you live could match the outline against
aerial imagery or parcel data. Redraw or crop the outline if that matters.
Zone titles and descriptions are kept; edit them first if they name people or
streets.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import secrets
import sys
from typing import Any

# Same projection constants as custom_components/groundskeeper/zone_bearing.py,
# so the translated copy gives identical local-metre geometry.
_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQ = 111320.0

_DECIMALS = 7  # about 1 cm
_FAKE_HASH_BASE = 10**18
_DROP_KEY_PARTS = (
    "device",
    "serial",
    "iot",
    "mac",
    "ssid",
    "address",
    "email",
    "token",
    "password",
    "account",
    "user",
)


def random_origin() -> tuple[float, float]:
    """A destination (lat, lon) unrelated to any input."""
    rng = secrets.SystemRandom()
    return rng.uniform(-50.0, 50.0), rng.uniform(-170.0, 170.0)


def _geometries(doc: dict[str, Any]):
    kind = doc.get("type")
    if kind == "FeatureCollection":
        for feature in doc.get("features", []):
            yield from _geometries(feature)
    elif kind == "Feature":
        if doc.get("geometry"):
            yield from _geometries(doc["geometry"])
    elif kind == "GeometryCollection":
        for geometry in doc.get("geometries", []):
            yield from _geometries(geometry)
    elif "coordinates" in doc:
        yield doc


def _points(coords: Any):
    if coords and isinstance(coords[0], (int, float)):
        yield coords
    else:
        for item in coords:
            yield from _points(item)


def _map_points(coords: Any, fn):
    if coords and isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_map_points(item, fn) for item in coords]


def _is_hash_key(key: str) -> bool:
    return key == "hash" or key.endswith("_hash") or key.startswith("parent_hash")


def _is_drop_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _DROP_KEY_PARTS)


def _walk_properties(node: Any):
    """Yield (container, key) for every dict entry under `node`."""
    if isinstance(node, dict):
        for key in list(node):
            yield node, key
            yield from _walk_properties(node[key])
    elif isinstance(node, list):
        for item in node:
            yield from _walk_properties(item)


def collect_hash_map(doc: dict[str, Any]) -> dict[str, str]:
    """Real hash (as text) -> fake hash (as text), in order of first appearance.

    Deterministic for a given document, so the same mapping can be applied to
    other files (tests that hard-code the real hashes).
    """
    mapping: dict[str, str] = {}
    for container, key in _walk_properties(doc.get("features", doc)):
        value = container[key]
        if _is_hash_key(key) and isinstance(value, (int, str)) and str(value).isdigit():
            text = str(value)
            if text != "0" and text not in mapping:
                mapping[text] = str(_FAKE_HASH_BASE + len(mapping) + 1)
    return mapping


def anonymize(
    doc: dict[str, Any],
    origin: tuple[float, float] | None = None,
    hash_map: dict[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[float, float]]:
    """Return (anonymized copy, destination (lat, lon))."""
    points = [p for g in _geometries(doc) for p in _points(g["coordinates"])]
    if not points:
        raise ValueError("no coordinates found in the GeoJSON")
    lat_t, lon_t = origin if origin is not None else random_origin()
    lon0 = sum(p[0] for p in points) / len(points)
    lat0 = sum(p[1] for p in points) / len(points)
    kx_in = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    kx_out = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat_t))

    def move(p: list[float]) -> list[float]:
        x = (p[0] - lon0) * kx_in
        y = (p[1] - lat0) * _M_PER_DEG_LAT
        return [
            round(lon_t + x / kx_out, _DECIMALS),
            round(lat_t + y / _M_PER_DEG_LAT, _DECIMALS),
        ]

    if hash_map is None:
        hash_map = collect_hash_map(doc)
    out = copy.deepcopy(doc)
    for geometry in _geometries(out):
        geometry["coordinates"] = _map_points(geometry["coordinates"], move)
    for node in (out, *out.get("features", [])):
        node.pop("bbox", None)
    for container, key in list(_walk_properties(out.get("features", out))):
        if key not in container:
            continue
        if _is_drop_key(key):
            del container[key]
        elif _is_hash_key(key) and str(container[key]) in hash_map:
            fake = hash_map[str(container[key])]
            container[key] = int(fake) if isinstance(container[key], int) else fake
    return out, (lat_t, lon_t)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", help="GeoJSON file, or - for stdin")
    parser.add_argument("-o", "--output", help="output file (default: stdout)")
    parser.add_argument(
        "--origin",
        metavar="LAT,LON",
        help="destination to move the map to (default: random)",
    )
    args = parser.parse_args(argv)

    origin = None
    if args.origin:
        lat, lon = (float(part) for part in args.origin.split(","))
        origin = (lat, lon)
    text = sys.stdin.read() if args.input == "-" else open(args.input).read()
    result, (lat_t, lon_t) = anonymize(json.loads(text), origin)
    rendered = json.dumps(result, indent=1) + "\n"
    if args.output:
        with open(args.output, "w") as handle:
            handle.write(rendered)
    else:
        sys.stdout.write(rendered)
    print(f"moved to {lat_t:.4f}, {lon_t:.4f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
