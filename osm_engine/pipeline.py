import os
import time
import shutil
import sqlite3
import subprocess
import zipfile
from typing import Dict, Any, Set
from .config import (
    RAM_TEMP_DIR, RAM_DB_DIR, EXPORT_DIR, ENABLE_PBF_COMPRESSION,
    TOP_LANGS
)
from .db import init_country_databases, create_db_indexes
from .extractor import FastUnifiedOSMExtractor


def process_country_target(c_info: Dict[str, Any], dynamic_languages: Dict[str, Set[str]]) -> Dict[str, Any]:
    cc = c_info["primary_cc"]
    slug = c_info["slug"]
    pbf_url = c_info["url"]

    # Sanitize slug and prefix to eliminate sub-directories (e.g. us/us-virgin-islands -> us_us-virgin-islands)
    safe_slug = slug.replace("/", "_").replace("\\", "_")
    prefix = cc if len(c_info["all_ccs"]) == 1 else f"{cc}_{safe_slug}"
    prefix = prefix.replace("/", "_").replace("\\", "_")

    # Local paths (stored on scratch disk)
    raw_pbf = os.path.join(RAM_TEMP_DIR, f"{safe_slug}.osm.pbf")
    filtered_pbf = os.path.join(RAM_TEMP_DIR, f"{safe_slug}_filtered.osm.pbf")
    local_raw_db = os.path.join(RAM_DB_DIR, f"{prefix}_osm.sqlite")
    local_spatial_db = os.path.join(RAM_DB_DIR, f"{prefix}_spatial.sqlite")

    # Export targets for GitHub Releases
    export_raw_db = os.path.join(EXPORT_DIR, f"{prefix}_osm.sqlite")
    export_spatial_db = os.path.join(EXPORT_DIR, f"{prefix}_spatial.sqlite")
    export_filtered_pbf = os.path.join(EXPORT_DIR, f"{prefix}_filtered.osm.pbf")
    export_bundle_zip = os.path.join(EXPORT_DIR, f"{prefix}_bundle.zip")

    t0 = time.time()

    # Pre-clean (including any stale -wal and -shm files)
    for f in (local_raw_db, local_spatial_db, raw_pbf, filtered_pbf, export_bundle_zip):
        for ext in ("", "-wal", "-shm"):
            target = f + ext
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass

    init_country_databases(local_raw_db, local_spatial_db)

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
    dl_res = subprocess.run(dl_cmd, capture_output=True, text=True)
    if dl_res.returncode != 0 or not os.path.exists(raw_pbf) or os.path.getsize(raw_pbf) == 0:
        err_msg = dl_res.stderr.strip() if dl_res.stderr else "Unknown download error"
        raise RuntimeError(f"Download failed from {pbf_url}: {err_msg}")

    dl_mb = os.path.getsize(raw_pbf) / (1024 * 1024)

    # 2. C++ Pre-filtering via Osmium Tool (Keep boundaries, places, admin levels, and ID references)
    filter_cmd = [
        "osmium", "tags-filter", raw_pbf,
        "r/boundary=administrative", "w/boundary=administrative",
        "r/type=boundary", "w/type=boundary",
        "nwr/place",
        "nwr/admin_level",
        "nwr/wikidata",
        "nwr/geonames:id",
        "nwr/gn:id"
    ]
    if not ENABLE_PBF_COMPRESSION:
        filter_cmd.extend(["-f", "pbf,pbf_compression=none"])
    filter_cmd.extend(["-o", filtered_pbf, "--overwrite"])

    filt_res = subprocess.run(filter_cmd, capture_output=True, text=True)
    if filt_res.returncode != 0:
        err_msg = filt_res.stderr.strip() if filt_res.stderr else "Unknown filter error"
        raise RuntimeError(f"Osmium filter failed for {slug}: {err_msg}")

    # Free raw download immediately to maintain scratch headroom
    if os.path.exists(raw_pbf):
        os.remove(raw_pbf)

    if not os.path.exists(filtered_pbf) or os.path.getsize(filtered_pbf) == 0:
        raise RuntimeError(f"Osmium filter generated empty output for {prefix}")

    filt_mb = os.path.getsize(filtered_pbf) / (1024 * 1024)

    # Copy pre-filtered PBF directly to export folder for fast future downstream processing
    shutil.copy(filtered_pbf, export_filtered_pbf)

    # 3. Stream & Extract into SQLite
    conn_raw = sqlite3.connect(local_raw_db, timeout=60.0)
    conn_s = sqlite3.connect(local_spatial_db, timeout=60.0)
    handler = FastUnifiedOSMExtractor(cc, conn_raw, conn_s, allowed_langs)
    handler.apply_file(filtered_pbf, locations=True, idx='flex_mem')
    handler.flush()
    handler.cleanup()  # Free way geometry coordinates, bboxes, and close cursors
    conn_raw.close()
    conn_s.close()

    # 4. Generate Spatial Indexes & Finalize WAL
    create_db_indexes(local_raw_db, local_spatial_db)

    # 5. Move Databases to Export Folder
    shutil.move(local_raw_db, export_raw_db)
    shutil.move(local_spatial_db, export_spatial_db)

    if os.path.exists(filtered_pbf):
        os.remove(filtered_pbf)

    # 6. Generate Minimized Country ZIP Bundle (contains both databases)
    with zipfile.ZipFile(export_bundle_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(export_raw_db, arcname=f"{prefix}_osm.sqlite")
        zf.write(export_spatial_db, arcname=f"{prefix}_spatial.sqlite")
    zip_mb = round(os.path.getsize(export_bundle_zip) / (1024 * 1024), 2)

    elapsed = time.time() - t0
    return {
        "cc": cc,
        "prefix": prefix,
        "name": c_info["name"],
        "raw_pbf_mb": round(dl_mb, 1),
        "filtered_pbf_mb": round(filt_mb, 1),
        "bundle_zip_mb": zip_mb,
        "entities": handler.total_raw_count,
        "polygons": handler.total_admin_polygons,
        "places": handler.total_spatial_places,
        "duration_sec": round(elapsed, 1)
    }
