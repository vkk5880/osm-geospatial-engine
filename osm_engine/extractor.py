import json
import sqlite3
from typing import Dict, Set, List, Tuple, Any, Optional
import osmium
import shapely.geometry
import shapely.ops
from .config import SETTLEMENT_PLACES, OSM_TYPE_MAP


def fast_geoname_id(tags) -> Optional[int]:
    for k in ("geonames:id", "gn:id", "geonames"):
        val = tags.get(k)
        if val:
            token = str(val).split(";")[0].split()[0]
            digits = "".join(c for c in token if c.isdigit())
            if digits:
                return int(digits)
    return None


def fast_wikidata_qid(tags) -> Optional[str]:
    val = tags.get("wikidata")
    if val:
        v = str(val).strip().upper().split(";")[0].strip()
        if v.startswith("Q") and v[1:].isdigit():
            return v
    return None


def fast_int(val: Any) -> Optional[int]:
    if not val:
        return None
    try:
        token = str(val).split(";")[0].split()[0]
        digits = "".join(c for c in token if c.isdigit())
        return int(digits) if digits else None
    except Exception:
        return None


def fast_float(val: Any) -> Optional[float]:
    if not val:
        return None
    try:
        token = str(val).split(";")[0].split()[0]
        cleaned = "".join(c for c in token if c.isdigit() or c in (".", "-"))
        return float(cleaned) if cleaned else None
    except Exception:
        return None


class FastUnifiedOSMExtractor(osmium.SimpleHandler):
    def __init__(self, country_code: str, conn_raw: sqlite3.Connection, conn_weather: sqlite3.Connection, allowed_langs: Set[str]):
        super().__init__()
        self.country_code = country_code
        self.conn_raw = conn_raw
        self.cur_raw = conn_raw.cursor()
        self.conn_w = conn_weather
        self.cur_w = conn_weather.cursor()
        self.allowed_langs = allowed_langs

        self.way_bboxes: Dict[int, Tuple[float, float, float, float]] = {}
        self.admin_way_coords: Dict[int, List[Tuple[float, float]]] = {}

        self.raw_entities: List[Tuple] = []
        self.raw_names: List[Tuple] = []
        self.raw_hierarchy: List[Tuple] = []

        self.weather_locations: List[Tuple] = []
        self.weather_names: List[Tuple] = []
        self.weather_hierarchy: List[Tuple] = []

        self.total_raw_count = 0
        self.total_weather_places = 0
        self.total_admin_polygons = 0
        self.total_hierarchy_links = 0

    def flush(self):
        if self.raw_entities:
            self.cur_raw.executemany("INSERT OR REPLACE INTO osm_entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);", self.raw_entities)
            self.raw_entities.clear()
        if self.raw_names:
            self.cur_raw.executemany("INSERT OR REPLACE INTO localized_names VALUES (?,?,?,?);", self.raw_names)
            self.raw_names.clear()
        if self.raw_hierarchy:
            self.cur_raw.executemany("INSERT OR IGNORE INTO osm_hierarchy VALUES (?,?,?,?,?);", self.raw_hierarchy)
            self.raw_hierarchy.clear()
        if self.weather_locations:
            self.cur_w.executemany("INSERT OR REPLACE INTO weather_locations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);", self.weather_locations)
            self.weather_locations.clear()
        if self.weather_names:
            self.cur_w.executemany("INSERT OR REPLACE INTO weather_names VALUES (?,?,?,?);", self.weather_names)
            self.weather_names.clear()
        if self.weather_hierarchy:
            self.cur_w.executemany("INSERT OR IGNORE INTO weather_hierarchy VALUES (?,?,?,?,?);", self.weather_hierarchy)
            self.weather_hierarchy.clear()

        self.conn_raw.commit()
        self.conn_w.commit()

    def _extract_names(self, osm_type: str, osm_id: int, tags, is_weather: bool):
        has_en = False
        for k, v in tags:
            val = str(v).strip()
            if not val:
                continue
            lcode = None
            if k.startswith("name:"):
                lcode = k[5:].lower()
            elif k.startswith("official_name:"):
                lcode = k[14:].lower()

            if lcode and lcode in self.allowed_langs:
                if lcode == "en":
                    has_en = True
                self.raw_names.append((osm_type, osm_id, lcode, val))
                if is_weather:
                    self.weather_names.append((osm_type, osm_id, lcode, val))

        if not has_en and "en" in self.allowed_langs:
            int_val = tags.get("int_name")
            if int_val and str(int_val).strip():
                clean_name = str(int_val).strip()
                self.raw_names.append((osm_type, osm_id, "en", clean_name))
                if is_weather:
                    self.weather_names.append((osm_type, osm_id, "en", clean_name))

    def node(self, n):
        tags = n.tags
        place = tags.get("place")
        admin_lvl = fast_int(tags.get("admin_level"))
        is_settlement = place in SETTLEMENT_PLACES
        gid = fast_geoname_id(tags)
        qid = fast_wikidata_qid(tags)

        if not (is_settlement or admin_lvl is not None or gid or qid):
            return

        lat, lon = (n.location.lat, n.location.lon) if n.location.valid() else (None, None)
        name = tags.get("name")
        pop = fast_int(tags.get("population"))
        ele = fast_float(tags.get("ele"))
        tz = tags.get("timezone")
        postcode = tags.get("postal_code") or tags.get("addr:postcode")
        wiki = tags.get("wikipedia")

        self.raw_entities.append((
            "node", n.id, gid, qid, name, self.country_code,
            lat, lon, place, admin_lvl, pop, ele, postcode, tz, wiki
        ))
        self.total_raw_count += 1

        is_weather = is_settlement or admin_lvl is not None or gid or qid
        if is_weather and lat is not None and lon is not None:
            self.weather_locations.append((
                "node", n.id, gid, qid, name, self.country_code,
                place or "administrative_point", admin_lvl, lat, lon,
                lat, lon, lat, lon, None, pop, ele, tz
            ))
            self.total_weather_places += 1

        self._extract_names("node", n.id, tags, is_weather)
        if len(self.raw_entities) >= 10000:
            self.flush()

    def way(self, w):
        tags = w.tags
        place = tags.get("place")
        admin_lvl = fast_int(tags.get("admin_level"))
        is_boundary = tags.get("boundary") == "administrative" or tags.get("type") == "boundary" or admin_lvl is not None
        is_settlement = place in SETTLEMENT_PLACES
        gid = fast_geoname_id(tags)
        qid = fast_wikidata_qid(tags)

        coords = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
        min_lat, min_lon, max_lat, max_lon = None, None, None, None
        lat, lon = None, None

        if coords:
            min_lon = min(c[0] for c in coords)
            min_lat = min(c[1] for c in coords)
            max_lon = max(c[0] for c in coords)
            max_lat = max(c[1] for c in coords)
            lat = (min_lat + max_lat) / 2.0
            lon = (min_lon + max_lon) / 2.0
            self.way_bboxes[w.id] = (min_lat, min_lon, max_lat, max_lon)
            if is_boundary:
                self.admin_way_coords[w.id] = coords

        if not (is_settlement or is_boundary or gid or qid):
            return

        name = tags.get("name")
        pop = fast_int(tags.get("population"))
        ele = fast_float(tags.get("ele"))
        tz = tags.get("timezone")
        postcode = tags.get("postal_code") or tags.get("addr:postcode")
        wiki = tags.get("wikipedia")

        self.raw_entities.append((
            "way", w.id, gid, qid, name, self.country_code,
            lat, lon, place, admin_lvl, pop, ele, postcode, tz, wiki
        ))
        self.total_raw_count += 1

        is_weather = is_settlement or is_boundary or gid or qid
        if is_weather and lat is not None and lon is not None:
            poly_geojson = None
            if is_boundary and coords and coords[0] == coords[-1] and len(coords) >= 4:
                poly_geojson = json.dumps({"type": "Polygon", "coordinates": [coords]})
                self.total_admin_polygons += 1
            else:
                self.total_weather_places += 1

            self.weather_locations.append((
                "way", w.id, gid, qid, name, self.country_code,
                "administrative" if is_boundary else (place or "settlement"),
                admin_lvl, lat, lon, min_lat, min_lon, max_lat, max_lon,
                poly_geojson, pop, ele, tz
            ))

        self._extract_names("way", w.id, tags, is_weather)
        if len(self.raw_entities) >= 10000:
            self.flush()

    def relation(self, r):
        tags = r.tags
        place = tags.get("place")
        admin_lvl = fast_int(tags.get("admin_level"))
        is_boundary = tags.get("boundary") == "administrative" or tags.get("type") == "boundary" or admin_lvl is not None
        gid = fast_geoname_id(tags)
        qid = fast_wikidata_qid(tags)

        if is_boundary or gid or qid:
            for m in r.members:
                child_type = OSM_TYPE_MAP.get(m.type, m.type)
                role = m.role or "member"
                self.raw_hierarchy.append(("relation", r.id, child_type, m.ref, role))
                if is_boundary or place in SETTLEMENT_PLACES:
                    self.weather_hierarchy.append(("relation", r.id, child_type, m.ref, role))
                self.total_hierarchy_links += 1

        if not (is_boundary or place in SETTLEMENT_PLACES or gid or qid):
            return

        m_lats, m_lons = [], []
        outer_coords = []
        for m in r.members:
            if m.type == 'w':
                if m.ref in self.way_bboxes:
                    w_min_lat, w_min_lon, w_max_lat, w_max_lon = self.way_bboxes[m.ref]
                    m_lats.extend([w_min_lat, w_max_lat])
                    m_lons.extend([w_min_lon, w_max_lon])
                if is_boundary and admin_lvl and admin_lvl <= 8 and m.role in ("outer", ""):
                    if m.ref in self.admin_way_coords:
                        outer_coords.append(self.admin_way_coords[m.ref])

        bbox_min_lat = min(m_lats) if m_lats else None
        bbox_max_lat = max(m_lats) if m_lats else None
        bbox_min_lon = min(m_lons) if m_lons else None
        bbox_max_lon = max(m_lons) if m_lons else None

        lat = (bbox_min_lat + bbox_max_lat) / 2.0 if bbox_min_lat is not None else None
        lon = (bbox_min_lon + bbox_max_lon) / 2.0 if bbox_min_lon is not None else None

        name = tags.get("name")
        pop = fast_int(tags.get("population"))
        ele = fast_float(tags.get("ele"))
        tz = tags.get("timezone")
        postcode = tags.get("postal_code") or tags.get("addr:postcode")
        wiki = tags.get("wikipedia")

        self.raw_entities.append((
            "relation", r.id, gid, qid, name, self.country_code,
            lat, lon, place, admin_lvl, pop, ele, postcode, tz, wiki
        ))
        self.total_raw_count += 1

        is_weather = is_boundary or place in SETTLEMENT_PLACES or gid or qid
        if is_weather:
            poly_geojson = None
            if is_boundary and outer_coords and (admin_lvl and admin_lvl <= 8):
                try:
                    lines = [shapely.geometry.LineString(c) for c in outer_coords if len(c) >= 2]
                    if lines:
                        merged = shapely.ops.linemerge(lines)
                        polygons = list(shapely.ops.polygonize(merged))
                        if polygons:
                            poly = shapely.geometry.MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]
                            poly_simplified = poly.simplify(0.002, preserve_topology=True)
                            poly_geojson = json.dumps(shapely.geometry.mapping(poly_simplified))
                            self.total_admin_polygons += 1
                except Exception:
                    poly_geojson = None

            if not poly_geojson:
                self.total_weather_places += 1

            self.weather_locations.append((
                "relation", r.id, gid, qid, name, self.country_code,
                "administrative" if is_boundary else (place or "settlement"),
                admin_lvl, lat, lon, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon,
                poly_geojson, pop, ele, tz
            ))

        self._extract_names("relation", r.id, tags, is_weather)
        if len(self.raw_entities) >= 10000:
            self.flush()

