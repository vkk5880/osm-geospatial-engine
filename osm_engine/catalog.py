from typing import Dict, Set, List, Any, Optional
import requests


def load_geonames_country_languages() -> Dict[str, Set[str]]:
    url = "https://download.geonames.org/export/dump/countryInfo.txt"
    resp = requests.get(url, timeout=20)
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


def get_geofabrik_targets(continent_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    idx_url = "https://download.geofabrik.de/index-v1.json"
    resp = requests.get(idx_url, timeout=30)
    data = resp.json()
    VALID_PARENTS = {
        "africa", "antarctica", "asia", "australia-oceania",
        "central-america", "europe", "north-america", "south-america"
    }
    targets = []
    seen_slugs = set()
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        parent = props.get("parent")
        pbf_url = props.get("urls", {}).get("pbf")
        iso_codes = props.get("iso3166-1:alpha2", [])
        slug_id = props.get("id")
        file_size_bytes = props.get("file_size", 0)

        if continent_filter and parent != continent_filter and slug_id != continent_filter:
            continue
        if pbf_url and (parent in VALID_PARENTS or slug_id == "russia") and iso_codes:
            if slug_id not in seen_slugs:
                seen_slugs.add(slug_id)
                clean_isos = [code.strip().upper() for code in iso_codes if code.strip()]
                size_mb = round(file_size_bytes / (1024 * 1024), 1) if file_size_bytes else 150.0
                targets.append({
                    "slug": slug_id,
                    "parent": parent,
                    "primary_cc": clean_isos[0],
                    "all_ccs": clean_isos,
                    "name": props.get("name"),
                    "url": pbf_url,
                    "size_mb": size_mb
                })
    targets.sort(key=lambda x: x["primary_cc"])
    return targets


def get_existing_release_assets(repo: str, tag: str, token: Optional[str] = None) -> Set[str]:
    """Fetches set of existing asset filenames from a GitHub release to support incremental runs."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OSM-Geospatial-Engine"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return {a.get("name") for a in data.get("assets", []) if a.get("name")}
    except Exception as e:
        print(f"⚠️ Could not fetch release assets for {tag}: {e}")
    return set()
