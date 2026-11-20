"""
One-off build of the spread map's county geometry (run again only if the boundary file changes):

    pip install topojson          # build-time only, not a runtime requirement
    python -m webapp.build_spread_geo

Reads the bundled Census 1:500k counties, keeps the 50 states and DC, and writes a
topology-preserving simplified TopoJSON (shared borders, so no slivers between counties;
the page derives state borders from it with topojson.mesh).
"""
import gzip
import json

import topojson
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.geometry.polygon import orient

from webapp.spread_sources import COUNTIES_PATH
from webapp.spread_view import STATIC_DIR, shown

OUT = STATIC_DIR / "spread" / "counties.topo.json"


MIN_PART_SQDEG = 0.0005  # ~5 km2, well under one pixel on the page; simplification collapses
                         # such slivers/islets into rings d3 draws inside-out (whole-map fill)


def _prepare(geometry: dict) -> dict:
    """Drop sub-pixel parts and wind exterior rings clockwise, as d3-geo requires."""
    g = shape(geometry)
    parts = list(g.geoms) if isinstance(g, MultiPolygon) else [g]
    keep = [p for p in parts if p.area >= MIN_PART_SQDEG] or [max(parts, key=lambda p: p.area)]
    keep = [orient(p, sign=-1.0) for p in keep]
    return mapping(MultiPolygon(keep) if len(keep) > 1 else keep[0])


def main() -> None:
    with gzip.open(COUNTIES_PATH, "rt", encoding="utf-8") as fh:
        gj = json.load(fh)
    features = [{"type": "Feature", "id": f["properties"]["fips"], "properties": {
                    "name": f["properties"]["name"], "st": f["properties"]["st"]},
                 "geometry": _prepare(f["geometry"])}
                for f in gj["features"] if shown(f["properties"]["fips"])]
    topo = topojson.Topology({"type": "FeatureCollection", "features": features},
                             object_name="counties", prequantize=1e5,
                             toposimplify=0.01, presimplify=False)
    OUT.write_text(topo.to_json(), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(features)} counties)")


if __name__ == "__main__":
    main()
