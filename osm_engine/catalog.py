from typing import Dict, Set, List, Any, Optional
import requests


def load_geonames_country_languages() -> Dict[str, Set[str]]:
    url = "https://download.geonames.org/export/dump/countryInfo.txt"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    country_langs: Dict[str, Set[str]] = {}
    for line in resp.text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 16:
            cc = parts[0].strip().upper()
            raw_langs = parts[15].strip()
            lang_set = set()
            if raw_langs:
                for lang_item in raw_langs.split(","):
                    base_lang = lang_item.split("-")[0].strip().lower()
                    if base_lang:
                        lang_set.add(base_lang)
            country_langs[cc] = lang_set
    return country_langs


# Approximate file sizes (MB) for large extracts to ensure balanced parallel runner scheduling
KNOWN_EXTRACT_SIZES_MB: Dict[str, float] = {
    "us": 9500.0,
    "united-states": 9500.0,
    "france": 4800.0,
    "germany": 4300.0,
    "russia": 4200.0,
    "canada": 3800.0,
    "brazil": 2700.0,
    "italy": 2400.0,
    "united-kingdom": 2000.0,
    "japan": 2000.0,
    "poland": 1900.0,
    "spain": 1600.0,
    "australia": 1400.0,
    "india": 1400.0,
    "indonesia": 900.0,
    "mexico": 800.0,
    "china": 800.0,
    "turkey": 650.0,
    "iran": 600.0,
    "ukraine": 550.0,
    "sweden": 500.0,
    "argentina": 450.0,
    "norway": 450.0,
    "finland": 400.0,
    "south-africa": 380.0,
    "new-zealand": 350.0,
    "colombia": 350.0,
    "vietnam": 320.0,
    "thailand": 300.0,
    "chile": 280.0,
    "philippines": 260.0,
    "austria": 250.0,
    "czech-republic": 250.0,
    "switzerland": 230.0,
    "greece": 220.0,
    "romania": 220.0,
    "gcc-states": 200.0,
    "netherlands": 200.0,
}


def get_geofabrik_targets(continent_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Discovers all country and territory extracts from Geofabrik, resolving continent lineage."""
    idx_url = "https://download.geofabrik.de/index-v1.json"
    resp = requests.get(idx_url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Build parent tree to trace hierarchical continent parents
    parent_map = {}
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        fid = props.get("id")
        parent_map[fid] = props.get("parent")

    CONTINENTS = {
        "africa", "antarctica", "asia", "australia-oceania",
        "central-america", "europe", "north-america", "south-america"
    }

    def resolve_continent(slug: str) -> Optional[str]:
        curr = slug
        for _ in range(10):
            p = parent_map.get(curr)
            if not p:
                break
            if p in CONTINENTS:
                return p
            curr = p
        return None

    targets = []
    seen_slugs = set()
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        pbf_url = props.get("urls", {}).get("pbf")
        iso_codes = props.get("iso3166-1:alpha2", [])
        slug_id = props.get("id")
        file_size_bytes = props.get("file_size", 0)

        # Country & autonomous territory extracts uniquely have iso3166-1:alpha2
        if not (pbf_url and iso_codes and slug_id not in seen_slugs):
            continue

        resolved_cont = resolve_continent(slug_id) or props.get("parent") or "other"
        if slug_id == "russia":
            resolved_cont = "russia"

        # Apply continent filter if specified (supports "all" or continent names)
        if continent_filter and continent_filter.lower() not in ("all", "worldwide"):
            filt = continent_filter.lower()
            if resolved_cont != filt and props.get("parent") != filt and slug_id != filt:
                continue

        seen_slugs.add(slug_id)
        clean_isos = [code.strip().upper() for code in iso_codes if code.strip()]
        size_mb = KNOWN_EXTRACT_SIZES_MB.get(slug_id, round(file_size_bytes / (1024 * 1024), 1) if file_size_bytes else 80.0)

        targets.append({
            "slug": slug_id,
            "parent": resolved_cont,
            "raw_parent": props.get("parent"),
            "primary_cc": clean_isos[0],
            "all_ccs": clean_isos,
            "name": props.get("name"),
            "url": pbf_url,
            "size_mb": size_mb
        })

    targets.sort(key=lambda x: x["primary_cc"])
    return targets


def get_existing_release_assets(repo: str, tag: str, token: Optional[str] = None) -> Set[str]:
    """Fetches all existing asset filenames from a GitHub release with pagination."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OSM-Geospatial-Engine"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    assets: Set[str] = set()
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return assets
        release_id = resp.json().get("id")
        if not release_id:
            return assets

        page = 1
        while True:
            assets_url = f"https://api.github.com/repos/{repo}/releases/{release_id}/assets?per_page=100&page={page}"
            r_page = requests.get(assets_url, headers=headers, timeout=15)
            if r_page.status_code != 200:
                break
            items = r_page.json()
            if not items:
                break
            for a in items:
                if a.get("name"):
                    assets.add(a["name"])
            if len(items) < 100:
                break
            page += 1
    except Exception as e:
        print(f"⚠️ Could not fetch release assets for {tag}: {e}")
    return assets
