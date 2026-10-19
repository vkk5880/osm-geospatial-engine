import os
import time
import shutil
import sqlite3
import subprocess
from typing import Dict, Any, Set
from .config import (
    RAM_TEMP_DIR, RAM_DB_DIR, EXPORT_DIR, ENABLE_PBF_COMPRESSION,
    SETTLEMENT_PLACES, TOP_LANGS
)
from .db import init_country_databases, create_db_indexes
from .extractor import FastUnifiedOSMExtractor


def process_country_target(c_info: Dict[str, Any], dynamic_languages: Dict[str, Set[str]]) -> Dict[str, Any]:
    cc = c_info["primary_cc"]
    slug = c_info["slug"]
    pbf_url = c_info["url"]

    # Local paths (stored on scratch disk)
    raw_pbf = os.path.join(RAM_TEMP_DIR, f"{slug}.osm.pbf")
    filtered_pbf = os.path.join(RAM_TEMP_DIR, f"{slug}_filtered.osm.pbf")
    local_raw_db = os.path.join(RAM_DB_DIR, f"{cc}_osm.sqlite")
    local_weather_db = os.path.join(RAM_DB_DIR, f"{cc}_weather.sqlite")

    # Export targets for GitHub Releases
    export_raw_db = os.path.join(EXPORT_DIR, f"{cc}_osm.sqlite")
    export_weather_db = os.path.join(EXPORT_DIR, f"{cc}_weather.sqlite")
    export_filtered_pbf = os.path.join(EXPORT_DIR, f"{cc}_filtered.osm.pbf")

    t0 = time.time()

    # Pre-clean
    for f in (local_raw_db, local_weather_db, raw_pbf, filtered_pbf):
        if os.path.exists(f):
            os.remove(f)

    init_country_databases(local_raw_db, local_weather_db)

    allowed_langs = set(TOP_LANGS)
    for iso in c_info["all_ccs"]:
        if iso in dynamic_languages:
            allowed_langs.update(dynamic_languages[iso])

    # 1. Download via multi-connection aria2c
    dl_cmd = [
        "aria2c", "-x", "4", "-s", "4", "-j", "4", "-k", "1M",
        "--allow-overwrite=true", "-d", RAM_TEMP_DIR, "-o",
        os.path.basename(raw_pbf), pbf_url
    ]
    res = subprocess.run(dl_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0 or not os.path.exists(raw_pbf) or os.path.getsize(raw_pbf) == 0:
        raise RuntimeError(f"Download failed from {pbf_url}")

    dl_mb = os.path.getsize(raw_pbf) / (1024 * 1024)

    # 2. C++ Pre-filtering via Osmium Tool
    places_arg = f"nwr/place={','.join(sorted(SETTLEMENT_PLACES))}"
    filter_cmd = [
        "osmium", "tags-filter", raw_pbf,
        "r/boundary=administrative", "w/boundary=administrative",
        "r/type=boundary", "w/type=boundary",
        places_arg
    ]
    if not ENABLE_PBF_COMPRESSION:
        filter_cmd.extend(["-f", "pbf,pbf_compression=none"])
    filter_cmd.extend(["-o", filtered_pbf, "--overwrite"])

    subprocess.run(filter_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Free raw download immediately to maintain scratch headroom
    if os.path.exists(raw_pbf):
        os.remove(raw_pbf)

    if not os.path.exists(filtered_pbf) or os.path.getsize(filtered_pbf) == 0:
        raise RuntimeError(f"Osmium filter generated empty output for {cc}")

    filt_mb = os.path.getsize(filtered_pbf) / (1024 * 1024)

    # Copy filtered PBF directly to export folder
    shutil.copy(filtered_pbf, export_filtered_pbf)

    # 3. Stream & Extract into SQLite
    conn_raw = sqlite3.connect(local_raw_db)
    conn_w = sqlite3.connect(local_weather_db)
    handler = FastUnifiedOSMExtractor(cc, conn_raw, conn_w, allowed_langs)
    handler.apply_file(filtered_pbf, locations=True, idx='flex_mem')
    handler.flush()
    conn_raw.close()
    conn_w.close()

    # 4. Generate Spatial Indexes
    create_db_indexes(local_raw_db, local_weather_db)

    # 5. Move Databases to Export Folder
    shutil.move(local_raw_db, export_raw_db)
    shutil.move(local_weather_db, export_weather_db)

    if os.path.exists(filtered_pbf):
        os.remove(filtered_pbf)

    elapsed = time.time() - t0
    return {
        "cc": cc,
        "name": c_info["name"],
        "raw_pbf_mb": round(dl_mb, 1),
        "filtered_pbf_mb": round(filt_mb, 1),
        "entities": handler.total_raw_count,
        "polygons": handler.total_admin_polygons,
        "places": handler.total_weather_places,
        "duration_sec": round(elapsed, 1)
    }

