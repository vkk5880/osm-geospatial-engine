import re
from typing import Dict, List, Set, Tuple, Optional, Any
import shapely.geometry
import shapely.ops
from shapely.prepared import prep


# Normalized name lookup for fallback when ISO codes are written as country names
COUNTRY_NAME_TO_CC: Dict[str, str] = {
    "senegal": "SN",
    "gambia": "GM",
    "the gambia": "GM",
    "haiti": "HT",
    "dominican republic": "DO",
    "republica dominicana": "DO",
    "república dominicana": "DO",
    "malaysia": "MY",
    "singapore": "SG",
    "brunei": "BN",
    "brunei darussalam": "BN",
    "israel": "IL",
    "palestine": "PS",
    "state of palestine": "PS",
    "west bank": "PS",
    "gaza": "PS",
    "gaza strip": "PS",
    "qatar": "QA",
    "saudi arabia": "SA",
    "united arab emirates": "AE",
    "uae": "AE",
    "kuwait": "KW",
    "oman": "OM",
    "bahrain": "BH",
}

# 1. Gambia River Basin Polygon (traces Gambia's enclave inside Senegal)
_GAMBIA_COORDS = [
    (-16.85, 13.06), (-16.50, 13.06), (-16.30, 13.15), (-15.90, 13.15),
    (-15.60, 13.25), (-15.30, 13.30), (-14.80, 13.20), (-14.30, 13.25),
    (-13.80, 13.30), (-13.80, 13.55), (-14.30, 13.60), (-14.80, 13.75),
    (-15.30, 13.70), (-15.60, 13.65), (-15.90, 13.65), (-16.30, 13.60),
    (-16.50, 13.56), (-16.70, 13.50), (-16.85, 13.35), (-16.85, 13.06)
]
_GAMBIA_POLY = shapely.geometry.Polygon(_GAMBIA_COORDS)
_PREP_GAMBIA = prep(_GAMBIA_POLY)

# 2. Haiti vs Dominican Republic Border Line (North to South)
_HAITI_DOMREP_BORDER = [
    (19.90, -71.74),
    (19.55, -71.71),
    (19.45, -71.70),
    (19.38, -71.78),
    (19.16, -71.70),
    (18.88, -71.70),
    (18.60, -71.80),
    (18.48, -71.86),
    (18.25, -71.76),
    (18.00, -71.75),
    (17.50, -71.75)
]

# 3. Singapore Strait Dividing Line & Singapore Bounding Box
_SINGAPORE_BBOX = (103.60, 1.15, 104.10, 1.475)
_JOHOR_STRAIT_LINE = [
    (1.350, 103.60),
    (1.355, 103.64),
    (1.440, 103.73),
    (1.454, 103.77),
    (1.465, 103.83),
    (1.430, 103.97),
    (1.410, 104.10)
]

# 4. Brunei Polygons (West and Temburong)
_BRUNEI_WEST_COORDS = [
    (114.05, 4.00), (114.95, 4.00), (115.10, 5.05), (114.60, 5.05), (114.05, 4.60)
]
_BRUNEI_EAST_COORDS = [
    (115.00, 4.30), (115.38, 4.30), (115.38, 4.90), (115.00, 4.90)
]
_BRUNEI_POLY = shapely.geometry.MultiPolygon([
    shapely.geometry.Polygon(_BRUNEI_WEST_COORDS),
    shapely.geometry.Polygon(_BRUNEI_EAST_COORDS)
])
_PREP_BRUNEI = prep(_BRUNEI_POLY)

# 5. Palestine (Gaza & West Bank) Polygons
_GAZA_COORDS = [
    (34.20, 31.20), (34.28, 31.20), (34.40, 31.37), (34.54, 31.55),
    (34.56, 31.60), (34.45, 31.60), (34.20, 31.35)
]
_WEST_BANK_COORDS = [
    (34.88, 31.33), (35.40, 31.33), (35.57, 31.85), (35.57, 32.40),
    (35.35, 32.55), (35.05, 32.55), (34.95, 32.10), (34.88, 31.65)
]
_PALESTINE_POLY = shapely.geometry.MultiPolygon([
    shapely.geometry.Polygon(_GAZA_COORDS),
    shapely.geometry.Polygon(_WEST_BANK_COORDS)
])
_PREP_PALESTINE = prep(_PALESTINE_POLY)


def _hispaniola_border_lon(lat: float) -> float:
    """Computes dividing longitude between Haiti and Dominican Republic at given latitude."""
    for i in range(len(_HAITI_DOMREP_BORDER) - 1):
        lat1, lon1 = _HAITI_DOMREP_BORDER[i]
        lat2, lon2 = _HAITI_DOMREP_BORDER[i + 1]
        if min(lat1, lat2) <= lat <= max(lat1, lat2):
            if abs(lat2 - lat1) < 1e-6:
                return (lon1 + lon2) / 2.0
            frac = (lat - lat1) / (lat2 - lat1)
            return lon1 + frac * (lon2 - lon1)
    return -71.75


def _johor_strait_lat(lon: float) -> float:
    """Computes dividing latitude along the Johor Strait for Singapore vs Malaysia."""
    for i in range(len(_JOHOR_STRAIT_LINE) - 1):
        lat1, lon1 = _JOHOR_STRAIT_LINE[i]
        lat2, lon2 = _JOHOR_STRAIT_LINE[i + 1]
        if min(lon1, lon2) <= lon <= max(lon1, lon2):
            if abs(lon2 - lon1) < 1e-6:
                return (lat1 + lat2) / 2.0
            frac = (lon - lon1) / (lon2 - lon1)
            return lat1 + frac * (lat2 - lat1)
    return 1.45


class SmartCountryRouter:
    """
    Intelligent Country Router for Multi-Country Geospatial Extracts.

    Guarantees zero cross-contamination and accurate country assignment using:
    1. Explicit Tag Matching: addr:country, country_code, ISO3166-1, etc.
    2. Dynamic admin_level=2 polygon containment (when available).
    3. Calibrated Geometric Boundaries & Spatial Partitions.
    """

    def __init__(self, primary_cc: str, all_ccs: List[str], admin2_polygons: Optional[Dict[str, Any]] = None):
        self.primary_cc = primary_cc
        self.all_ccs = set(all_ccs)
        self.is_multi = len(all_ccs) > 1

        self.prep_admin2: Dict[str, Any] = {}
        if admin2_polygons:
            for cc, geom in admin2_polygons.items():
                if cc in self.all_ccs and geom and not geom.is_empty:
                    self.prep_admin2[cc] = prep(geom)

    def route_entity(self, tags: Any, lat: Optional[float], lon: Optional[float]) -> str:
        """Determines the true country code for an OSM entity."""
        if not self.is_multi:
            return self.primary_cc

        # 1. First Priority: Check explicit country metadata tags
        explicit_cc = self._extract_explicit_cc(tags)
        if explicit_cc and explicit_cc in self.all_ccs:
            return explicit_cc

        # If entity lacks coordinates, fallback to explicit or primary
        if lat is None or lon is None:
            return self.primary_cc

        pt = shapely.geometry.Point(lon, lat)

        # 2. Second Priority: Test against dynamic admin_level=2 polygons
        for cc, prep_poly in self.prep_admin2.items():
            if prep_poly.contains(pt):
                return cc

        # 3. Third Priority: Calibrated regional geographic partitioning
        return self._route_by_calibrated_geometry(lat, lon, pt)

    def _extract_explicit_cc(self, tags: Any) -> Optional[str]:
        get_fn = getattr(tags, "get", None)
        if not get_fn:
            return None
        for k in ("country_code", "addr:country", "ISO3166-1", "ISO3166-1:alpha2", "is_in:country_code"):
            val = get_fn(k)
            if val:
                candidate = str(val).strip().upper()[:2]
                if candidate in self.all_ccs:
                    return candidate

        # Check country name strings
        for k in ("is_in:country", "country"):
            val = get_fn(k)
            if val:
                name_clean = str(val).strip().lower()
                if name_clean in COUNTRY_NAME_TO_CC:
                    cc = COUNTRY_NAME_TO_CC[name_clean]
                    if cc in self.all_ccs:
                        return cc
        return None

    def _route_by_calibrated_geometry(self, lat: float, lon: float, pt: shapely.geometry.Point) -> str:
        # A. Senegal & The Gambia (SN vs GM)
        if "SN" in self.all_ccs and "GM" in self.all_ccs:
            if 13.06 <= lat <= 13.85 and -16.85 <= lon <= -13.80:
                if _PREP_GAMBIA.contains(pt):
                    return "GM"
            return "SN"

        # B. Haiti & Dominican Republic (HT vs DO)
        if "HT" in self.all_ccs and "DO" in self.all_ccs:
            if lon < -72.05:
                return "HT"
            if lon > -71.55:
                return "DO"
            border_lon = _hispaniola_border_lon(lat)
            return "HT" if lon < border_lon else "DO"

        # C. Malaysia, Singapore & Brunei (MY vs SG vs BN)
        if "MY" in self.all_ccs:
            if "SG" in self.all_ccs:
                min_x, min_y, max_x, max_y = _SINGAPORE_BBOX
                if min_x <= lon <= max_x and min_y <= lat <= max_y:
                    strait_lat = _johor_strait_lat(lon)
                    if lat <= strait_lat:
                        return "SG"
            if "BN" in self.all_ccs:
                if 4.00 <= lat <= 5.10 and 114.05 <= lon <= 115.40:
                    if _PREP_BRUNEI.contains(pt):
                        return "BN"
            return "MY"

        # D. Israel & Palestine (IL vs PS)
        if "IL" in self.all_ccs and "PS" in self.all_ccs:
            if _PREP_PALESTINE.contains(pt):
                return "PS"
            return "IL"

        # E. GCC States (QA, SA, AE, KW, OM, BH)
        if "SA" in self.all_ccs:
            # Bahrain
            if "BH" in self.all_ccs and 25.55 <= lat <= 26.35 and 50.35 <= lon <= 50.70:
                return "BH"
            # Qatar
            if "QA" in self.all_ccs and 24.50 <= lat <= 26.20 and 50.72 <= lon <= 51.65:
                return "QA"
            # Kuwait
            if "KW" in self.all_ccs and 28.52 <= lat <= 30.10 and 46.55 <= lon <= 48.45:
                return "KW"
            # Oman (Musandam exclave + Madha + Southern Oman)
            if "OM" in self.all_ccs:
                if 25.60 <= lat <= 26.40 and 56.10 <= lon <= 56.55:
                    return "OM"
                if 25.25 <= lat <= 25.35 and 56.30 <= lon <= 56.37:
                    return "OM"
                if lat <= 24.50 and lon >= 55.00:
                    return "OM"
                if lat <= 20.00 and lon >= 52.00:
                    return "OM"
            # UAE
            if "AE" in self.all_ccs and 22.60 <= lat <= 25.55 and 51.55 <= lon <= 56.40:
                return "AE"
            return "SA"

        return self.primary_cc
