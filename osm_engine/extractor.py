import json
import sqlite3
from typing import Dict, Set, List, Tuple, Any, Optional
import osmium
import shapely.geometry
import shapely.ops
from .config import SETTLEMENT_PLACES, OSM_TYPE_MAP
from .router import SmartCountryRouter, COUNTRY_NAME_TO_CC


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
    """Safely parses integers handling commas ('1,250,000') and decimal strings ('15000.0')."""
    if not val:
        return None
    try:
        token = str(val).split(";")[0].strip().split()[0].replace(",", "")
        return int(float(token))
    except (ValueError, TypeError, IndexError):
        return None


def fast_float(val: Any) -> Optional[float]:
    """Safely parses floats handling European comma decimals ('1250,5' -> 1250.5)."""
    if not val:
        return None
    try:
        token = str(val).split(";")[0].strip().split()[0].replace(",", ".")
        return float(token)
    except (ValueError, TypeError, IndexError):
        return None


class AdminBoundaryCollector(osmium.SimpleHandler):
    """
    Ultra-fast pre-pass on pre-filtered PBF to extract admin_level=2 national boundary
    polygons for constituent countries in multi-country extracts.
    """
    def __init__(self, target_ccs: Set[str]):
        super().__init__()
        self.target_ccs = set(target_ccs)
        self.admin_way_coords: Dict[int, List[Tuple[float, float]]] = {}
        self.polygons: Dict[str, Any] = {}

    def way(self, w):
        tags = w.tags
        is_boundary = tags.get("boundary") == "administrative" or tags.get("type") == "boundary" or tags.get("admin_level") == "2"
        if is_boundary:
            coords = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
            if coords:
                self.admin_way_coords[w.id] = coords

    def relation(self, r):
        tags = r.tags
        if tags.get("admin_level") == "2" and (tags.get("boundary") == "administrative" or tags.get("type") == "boundary"):
            rel_cc = None
            for k in ("ISO3166-1", "ISO3166-1:alpha2", "country_code"):
                v = tags.get(k)
                if v:
                    cand = str(v).strip().upper()[:2]
                    if cand in self.target_ccs:
                        rel_cc = cand
                        break
            if not rel_cc:
                name_clean = str(tags.get("name") or "").strip().lower()
                cand = COUNTRY_NAME_TO_CC.get(name_clean)
                if cand in self.target_ccs:
                    rel_cc = cand

            if rel_cc and rel_cc not in self.polygons:
                outer_coords = []
                for m in r.members:
                    if m.type == 'w' and m.role in ("outer", ""):
                        if m.ref in self.admin_way_coords:
                            outer_coords.append(self.admin_way_coords[m.ref])
                if outer_coords:
                    try:
                        lines = [shapely.geometry.LineString(c) for c in outer_coords if len(c) >= 2]
                        if lines:
                            merged = shapely.ops.linemerge(lines)
                            polys = list(shapely.ops.polygonize(merged))
                            if polys:
                                poly = shapely.geometry.MultiPolygon(polys) if len(polys) > 1 else polys[0]
                                self.polygons[rel_cc] = poly.simplify(0.005, preserve_topology=True)
                    except Exception:
                        pass


class FastUnifiedOSMExtractor(osmium.SimpleHandler):
    """
    High-Throughput Unified OSM Ingestion Engine.
    Streams nodes, ways, and relations into self-contained SQLite databases.
    Seamlessly partitions multi-country extracts into isolated per-country databases.
    """

    def __init__(
        self,
        country_code: str,
        conn_raw: Any,
        conn_spatial: Any,
        allowed_langs: Set[str],
        all_ccs: Optional[List[str]] = None,
        admin2_polygons: Optional[Dict[str, Any]] = None
    ):
        super().__init__()
        self.primary_cc = country_code
        self.all_ccs = all_ccs or [country_code]
        self.allowed_langs = allowed_langs
        self.router = SmartCountryRouter(country_code, self.all_ccs, admin2_polygons)

        # Support both single DB connection and multi-country connection dicts
        if isinstance(conn_raw, dict):
            self.conn_raw_map = conn_raw
            self.conn_s_map = conn_spatial
        else:
            self.conn_raw_map = {self.primary_cc: conn_raw}
            self.conn_s_map = {self.primary_cc: conn_spatial}

        self.cur_raw_map = {cc: conn.cursor() for cc, conn in self.conn_raw_map.items()}
        self.cur_s_map = {cc: conn.cursor() for cc, conn in self.conn_s_map.items()}

        self.way_bboxes: Dict[int, Tuple[float, float, float, float]] = {}
        self.admin_way_coords: Dict[int, List[Tuple[float, float]]] = {}

        # Buffered inserts partitioned by target country code
        self.raw_entities: Dict[str, List[Tuple]] = {cc: [] for cc in self.all_ccs}
        self.raw_names: Dict[str, List[Tuple]] = {cc: [] for cc in self.all_ccs}
        self.raw_hierarchy: Dict[str, List[Tuple]] = {cc: [] for cc in self.all_ccs}

        self.spatial_locations: Dict[str, List[Tuple]] = {cc: [] for cc in self.all_ccs}
        self.spatial_names: Dict[str, List[Tuple]] = {cc: [] for cc in self.all_ccs}
        self.spatial_hierarchy: Dict[str, List[Tuple]] = {cc: [] for cc in self.all_ccs}

        # Bounding box per country (min_lon, min_lat, max_lon, max_lat)
        self.country_bounds: Dict[str, List[float]] = {
            cc: [180.0, 90.0, -180.0, -90.0] for cc in self.all_ccs
        }

        self.total_raw_count = 0
        self.total_spatial_places = 0
        self.total_admin_polygons = 0
        self.total_hierarchy_links = 0
        self._buffered_count = 0

        self.country_raw_count: Dict[str, int] = {cc: 0 for cc in self.all_ccs}
        self.country_spatial_count: Dict[str, int] = {cc: 0 for cc in self.all_ccs}
        self.country_polygons_count: Dict[str, int] = {cc: 0 for cc in self.all_ccs}

    def cleanup(self):
        """Releases geometry cache from memory and closes cursors once stream processing is complete."""
        self.way_bboxes.clear()
        self.admin_way_coords.clear()
        for cur in self.cur_raw_map.values():
            try:
                cur.close()
            except Exception:
                pass
        for cur in self.cur_s_map.values():
            try:
                cur.close()
            except Exception:
                pass

    def _update_country_bounds(self, cc: str, lat: float, lon: float):
        b = self.country_bounds.get(cc)
        if b is not None:
            if lon < b[0]: b[0] = lon
            if lat < b[1]: b[1] = lat
            if lon > b[2]: b[2] = lon
            if lat > b[3]: b[3] = lat

    def flush(self):
        for cc in self.all_ccs:
            if self.raw_entities[cc]:
                self.cur_raw_map[cc].executemany("INSERT OR REPLACE INTO osm_entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);", self.raw_entities[cc])
                self.raw_entities[cc].clear()
            if self.raw_names[cc]:
                self.cur_raw_map[cc].executemany("INSERT OR REPLACE INTO localized_names VALUES (?,?,?,?,?);", self.raw_names[cc])
                self.raw_names[cc].clear()
            if self.raw_hierarchy[cc]:
                self.cur_raw_map[cc].executemany("INSERT OR IGNORE INTO osm_hierarchy VALUES (?,?,?,?,?);", self.raw_hierarchy[cc])
                self.raw_hierarchy[cc].clear()

            if self.spatial_locations[cc]:
                self.cur_s_map[cc].executemany("INSERT OR REPLACE INTO spatial_locations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);", self.spatial_locations[cc])
                self.spatial_locations[cc].clear()
            if self.spatial_names[cc]:
                self.cur_s_map[cc].executemany("INSERT OR REPLACE INTO spatial_names VALUES (?,?,?,?,?);", self.spatial_names[cc])
                self.spatial_names[cc].clear()
            if self.spatial_hierarchy[cc]:
                self.cur_s_map[cc].executemany("INSERT OR IGNORE INTO spatial_hierarchy VALUES (?,?,?,?,?);", self.spatial_hierarchy[cc])
                self.spatial_hierarchy[cc].clear()

            self.conn_raw_map[cc].commit()
            self.conn_s_map[cc].commit()

    def _extract_names(self, target_cc: str, osm_type: str, osm_id: int, tags, is_spatial: bool):
        """Extracts both common and official localized names so no translation data is lost."""
        for k, v in tags:
            val = str(v).strip()
            if not val:
                continue
            if k.startswith("name:"):
                lcode = k[5:].lower()
                if lcode in self.allowed_langs:
                    self.raw_names[target_cc].append((osm_type, osm_id, lcode, "common", val))
                    if is_spatial:
                        self.spatial_names[target_cc].append((osm_type, osm_id, lcode, "common", val))
            elif k.startswith("official_name:"):
                lcode = k[14:].lower()
                if lcode in self.allowed_langs:
                    self.raw_names[target_cc].append((osm_type, osm_id, lcode, "official", val))
                    if is_spatial:
                        self.spatial_names[target_cc].append((osm_type, osm_id, lcode, "official", val))

        int_val = tags.get("int_name")
        if int_val and str(int_val).strip() and "en" in self.allowed_langs:
            clean_name = str(int_val).strip()
            self.raw_names[target_cc].append((osm_type, osm_id, "en", "international", clean_name))
            if is_spatial:
                self.spatial_names[target_cc].append((osm_type, osm_id, "en", "international", clean_name))

    def node(self, n):
        tags = n.tags
        place = tags.get("place")
        admin_lvl = fast_int(tags.get("admin_level"))
        is_settlement = place in SETTLEMENT_PLACES or (place is not None and place not in ("no", "isolated_dwelling"))
        gid = fast_geoname_id(tags)
        qid = fast_wikidata_qid(tags)

        if not (is_settlement or admin_lvl is not None or gid or qid):
            return

        lat, lon = (n.location.lat, n.location.lon) if n.location.valid() else (None, None)
        target_cc = self.router.route_entity(tags, lat, lon)
        if target_cc not in self.all_ccs:
            target_cc = self.primary_cc

        if lat is not None and lon is not None:
            self._update_country_bounds(target_cc, lat, lon)

        name = tags.get("name")
        pop = fast_int(tags.get("population"))
        ele = fast_float(tags.get("ele"))
        tz = tags.get("timezone")
        postcode = tags.get("postal_code") or tags.get("addr:postcode")
        wiki = tags.get("wikipedia")

        self.raw_entities[target_cc].append((
            "node", n.id, gid, qid, name, target_cc,
            lat, lon, place, admin_lvl, pop, ele, postcode, tz, wiki
        ))
        self.total_raw_count += 1
        self.country_raw_count[target_cc] += 1

        is_spatial = is_settlement or admin_lvl is not None or gid or qid
        if is_spatial and lat is not None and lon is not None:
            self.spatial_locations[target_cc].append((
                "node", n.id, gid, qid, name, target_cc,
                place or "administrative_point", admin_lvl, lat, lon,
                lat, lon, lat, lon, None, pop, ele, tz
            ))
            self.total_spatial_places += 1
            self.country_spatial_count[target_cc] += 1

        self._extract_names(target_cc, "node", n.id, tags, is_spatial)
        self._buffered_count += 1
        if self._buffered_count >= 15000:
            self.flush()
            self._buffered_count = 0

    def way(self, w):
        tags = w.tags
        place = tags.get("place")
        admin_lvl = fast_int(tags.get("admin_level"))
        is_boundary = tags.get("boundary") == "administrative" or tags.get("type") == "boundary" or admin_lvl is not None
        is_settlement = place in SETTLEMENT_PLACES or (place is not None and place not in ("no", "isolated_dwelling"))
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
            self.admin_way_coords[w.id] = coords

        if not (is_settlement or is_boundary or gid or qid):
            return

        target_cc = self.router.route_entity(tags, lat, lon)
        if target_cc not in self.all_ccs:
            target_cc = self.primary_cc

        if lat is not None and lon is not None:
            self._update_country_bounds(target_cc, lat, lon)

        name = tags.get("name")
        pop = fast_int(tags.get("population"))
        ele = fast_float(tags.get("ele"))
        tz = tags.get("timezone")
        postcode = tags.get("postal_code") or tags.get("addr:postcode")
        wiki = tags.get("wikipedia")

        self.raw_entities[target_cc].append((
            "way", w.id, gid, qid, name, target_cc,
            lat, lon, place, admin_lvl, pop, ele, postcode, tz, wiki
        ))
        self.total_raw_count += 1
        self.country_raw_count[target_cc] += 1

        is_spatial = is_settlement or is_boundary or gid or qid
        if is_spatial and lat is not None and lon is not None:
            poly_geojson = None
            if is_boundary and coords and coords[0] == coords[-1] and len(coords) >= 4:
                poly_geojson = json.dumps({"type": "Polygon", "coordinates": [coords]})
                self.total_admin_polygons += 1
                self.country_polygons_count[target_cc] += 1
            else:
                self.total_spatial_places += 1
                self.country_spatial_count[target_cc] += 1

            self.spatial_locations[target_cc].append((
                "way", w.id, gid, qid, name, target_cc,
                "administrative" if is_boundary else (place or "settlement"),
                admin_lvl, lat, lon, min_lat, min_lon, max_lat, max_lon,
                poly_geojson, pop, ele, tz
            ))

        self._extract_names(target_cc, "way", w.id, tags, is_spatial)
        self._buffered_count += 1
        if self._buffered_count >= 15000:
            self.flush()
            self._buffered_count = 0

    def relation(self, r):
        tags = r.tags
        place = tags.get("place")
        admin_lvl = fast_int(tags.get("admin_level"))
        is_boundary = tags.get("boundary") == "administrative" or tags.get("type") == "boundary" or admin_lvl is not None
        gid = fast_geoname_id(tags)
        qid = fast_wikidata_qid(tags)

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

        target_cc = self.router.route_entity(tags, lat, lon)
        if target_cc not in self.all_ccs:
            target_cc = self.primary_cc

        if lat is not None and lon is not None:
            self._update_country_bounds(target_cc, lat, lon)

        # Hierarchy recorded strictly for the target country's database
        if is_boundary or gid or qid:
            for m in r.members:
                if m.type in ('r', 'n') or m.role in ('admin_centre', 'label', 'subarea'):
                    child_type = OSM_TYPE_MAP.get(m.type, m.type)
                    role = m.role or "member"
                    self.raw_hierarchy[target_cc].append(("relation", r.id, child_type, m.ref, role))
                    if is_boundary or (place and place in SETTLEMENT_PLACES):
                        self.spatial_hierarchy[target_cc].append(("relation", r.id, child_type, m.ref, role))
                    self.total_hierarchy_links += 1

        if not (is_boundary or place in SETTLEMENT_PLACES or gid or qid):
            return

        name = tags.get("name")
        pop = fast_int(tags.get("population"))
        ele = fast_float(tags.get("ele"))
        tz = tags.get("timezone")
        postcode = tags.get("postal_code") or tags.get("addr:postcode")
        wiki = tags.get("wikipedia")

        is_spatial = is_boundary or place in SETTLEMENT_PLACES or gid or qid
        poly_geojson = None
        if is_spatial and is_boundary and outer_coords and (admin_lvl and admin_lvl <= 8):
            try:
                lines = [shapely.geometry.LineString(c) for c in outer_coords if len(c) >= 2]
                if lines:
                    merged = shapely.ops.linemerge(lines)
                    polygons = list(shapely.ops.polygonize(merged))
                    if polygons:
                        poly = shapely.geometry.MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]
                        poly_simplified = poly.simplify(0.002, preserve_topology=True)
                        rep_pt = poly_simplified.representative_point()
                        lat, lon = round(rep_pt.y, 6), round(rep_pt.x, 6)
                        poly_geojson = json.dumps(shapely.geometry.mapping(poly_simplified))
                        self.total_admin_polygons += 1
                        self.country_polygons_count[target_cc] += 1
            except Exception:
                poly_geojson = None

        self.raw_entities[target_cc].append((
            "relation", r.id, gid, qid, name, target_cc,
            lat, lon, place, admin_lvl, pop, ele, postcode, tz, wiki
        ))
        self.total_raw_count += 1
        self.country_raw_count[target_cc] += 1

        if is_spatial:
            if not poly_geojson:
                self.total_spatial_places += 1
                self.country_spatial_count[target_cc] += 1

            self.spatial_locations[target_cc].append((
                "relation", r.id, gid, qid, name, target_cc,
                "administrative" if is_boundary else (place or "settlement"),
                admin_lvl, lat, lon, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon,
                poly_geojson, pop, ele, tz
            ))

        self._extract_names(target_cc, "relation", r.id, tags, is_spatial)
        self._buffered_count += 1
        if self._buffered_count >= 15000:
            self.flush()
            self._buffered_count = 0
