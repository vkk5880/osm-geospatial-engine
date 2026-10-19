import os
import time
import shutil
import sqlite3
import subprocess
import zipfile
from typing import Dict, Any, Set, List
from .config import (
    RAM_TEMP_DIR, RAM_DB_DIR, EXPORT_DIR, ENABLE_PBF_COMPRESSION,
    TOP_LANGS
)
from .db import init_country_databases, create_db_indexes
from .extractor import FastUnifiedOSMExtractor, AdminBoundaryCollector


def process_country_target(c_info: Dict[str, Any], dynamic_languages: Dict[str, Set[str]]) -> Dict[str, Any]:
    primary_cc = c_info["primary_cc"]
    all_ccs = c_info.get("all_ccs") or [primary_cc]
    is_multi = len(all_ccs) > 1
    slug = c_info["slug"]
    pbf_url = c_info["url"]

    # Sanitize slug
    safe_slug = slug.replace("/", "_").replace("\\", "_")

    # Local scratch paths
    raw_pbf = os.path.join(RAM_TEMP_DIR, f"{safe_slug}.osm.pbf")
    filtered_pbf = os.path.join(RAM_TEMP_DIR, f"{safe_slug}_filtered.osm.pbf")

    # Per-country database and export paths
    local_raw_dbs: Dict[str, str] = {}
    local_spatial_dbs: Dict[str, str] = {}
    export_raw_dbs: Dict[str, str] = {}
    export_spatial_dbs: Dict[str, str] = {}
    export_filtered_pbfs: Dict[str, str] = {}
    export_bundle_zips: Dict[str, str] = {}

    for sub_cc in all_ccs:
        local_raw_dbs[sub_cc] = os.path.join(RAM_DB_DIR, f"{sub_cc}_osm.sqlite")
        local_spatial_dbs[sub_cc] = os.path.join(RAM_DB_DIR, f"{sub_cc}_spatial.sqlite")
        export_raw_dbs[sub_cc] = os.path.join(EXPORT_DIR, f"{sub_cc}_osm.sqlite")
        export_spatial_dbs[sub_cc] = os.path.join(EXPORT_DIR, f"{sub_cc}_spatial.sqlite")
        export_filtered_pbfs[sub_cc] = os.path.join(EXPORT_DIR, f"{sub_cc}_filtered.osm.pbf")
        export_bundle_zips[sub_cc] = os.path.join(EXPORT_DIR, f"{sub_cc}_bundle.zip")

    t0 = time.time()

    # Pre-clean stale files (including -wal and -shm)
    files_to_clean = [raw_pbf, filtered_pbf]
    for sub_cc in all_ccs:
        files_to_clean.extend([
            local_raw_dbs[sub_cc], local_spatial_dbs[sub_cc],
            export_bundle_zips[sub_cc], export_filtered_pbfs[sub_cc]
        ])

    for f in files_to_clean:
        for ext in ("", "-wal", "-shm"):
            target = f + ext
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass

    # Initialize schema for each target country database
    for sub_cc in all_ccs:
        init_country_databases(local_raw_dbs[sub_cc], local_spatial_dbs[sub_cc])

    allowed_langs = set(TOP_LANGS)
    for iso in all_ccs:
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
        raise RuntimeError(f"Osmium filter generated empty output for {safe_slug}")

    filt_mb = os.path.getsize(filtered_pbf) / (1024 * 1024)

    # 3. Dynamic Boundary Collector (Extract admin_level=2 polygons if multi-country)
    admin2_polys = None
    if is_multi:
        try:
            collector = AdminBoundaryCollector(set(all_ccs))
            collector.apply_file(filtered_pbf, locations=True, idx='flex_mem')
            admin2_polys = collector.polygons
        except Exception as e:
            print(f"⚠️ Notice: Admin boundary pre-collector skipped ({e}), falling back to calibrated geometries.")

    # 4. Stream & Extract into Partitioned SQLite Databases
    conn_raw_map = {sub_cc: sqlite3.connect(local_raw_dbs[sub_cc], timeout=60.0) for sub_cc in all_ccs}
    conn_s_map = {sub_cc: sqlite3.connect(local_spatial_dbs[sub_cc], timeout=60.0) for sub_cc in all_ccs}

    handler = FastUnifiedOSMExtractor(
        country_code=primary_cc,
        conn_raw=conn_raw_map,
        conn_spatial=conn_s_map,
        allowed_langs=allowed_langs,
        all_ccs=all_ccs,
        admin2_polygons=admin2_polys
    )
    handler.apply_file(filtered_pbf, locations=True, idx='flex_mem')
    handler.flush()
    handler.cleanup()  # Free way geometries, bboxes, and close cursors

    for conn in conn_raw_map.values():
        conn.close()
    for conn in conn_s_map.values():
        conn.close()

    # 5. Build Indexes, Package ZIP Bundles, and Export
    bundle_sizes: Dict[str, float] = {}
    for sub_cc in all_ccs:
        # Generate Spatial Indexes & Finalize WAL
        create_db_indexes(local_raw_dbs[sub_cc], local_spatial_dbs[sub_cc])

        # Move to Export Folder
        shutil.move(local_raw_dbs[sub_cc], export_raw_dbs[sub_cc])
        shutil.move(local_spatial_dbs[sub_cc], export_spatial_dbs[sub_cc])

        # Generate Minimized Country ZIP Bundle (contains both databases)
        with zipfile.ZipFile(export_bundle_zips[sub_cc], "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(export_raw_dbs[sub_cc], arcname=f"{sub_cc}_osm.sqlite")
            zf.write(export_spatial_dbs[sub_cc], arcname=f"{sub_cc}_spatial.sqlite")
        bundle_sizes[sub_cc] = round(os.path.getsize(export_bundle_zips[sub_cc]) / (1024 * 1024), 2)

        # Generate Per-Country Filtered PBF
        if not is_multi:
            shutil.copy(filtered_pbf, export_filtered_pbfs[sub_cc])
        else:
            bounds = handler.country_bounds.get(sub_cc)
            if bounds and bounds[0] <= bounds[2] and bounds[1] <= bounds[3] and bounds[0] != 180.0:
                # Add 0.05 degree margin
                min_lon = max(-180.0, bounds[0] - 0.05)
                min_lat = max(-90.0, bounds[1] - 0.05)
                max_lon = min(180.0, bounds[2] + 0.05)
                max_lat = min(90.0, bounds[3] + 0.05)
                bbox_str = f"{min_lon:.4f},{min_lat:.4f},{max_lon:.4f},{max_lat:.4f}"
                extract_cmd = ["osmium", "extract", "-b", bbox_str, filtered_pbf, "-o", export_filtered_pbfs[sub_cc], "--overwrite"]
                res = subprocess.run(extract_cmd, capture_output=True, text=True)
                if res.returncode != 0 or not os.path.exists(export_filtered_pbfs[sub_cc]) or os.path.getsize(export_filtered_pbfs[sub_cc]) == 0:
                    shutil.copy(filtered_pbf, export_filtered_pbfs[sub_cc])
            else:
                shutil.copy(filtered_pbf, export_filtered_pbfs[sub_cc])

    if os.path.exists(filtered_pbf):
        os.remove(filtered_pbf)

    elapsed = time.time() - t0
    total_bundle_mb = round(sum(bundle_sizes.values()), 2)
    return {
        "cc": primary_cc if not is_multi else ",".join(all_ccs),
        "prefix": primary_cc if not is_multi else safe_slug,
        "name": c_info["name"],
        "all_ccs": all_ccs,
        "raw_pbf_mb": round(dl_mb, 1),
        "filtered_pbf_mb": round(filt_mb, 1),
        "bundle_zip_mb": total_bundle_mb,
        "bundle_sizes": bundle_sizes,
        "entities": handler.total_raw_count,
        "polygons": handler.total_admin_polygons,
        "places": handler.total_spatial_places,
        "country_raw_count": handler.country_raw_count,
        "country_spatial_count": handler.country_spatial_count,
        "duration_sec": round(elapsed, 1)
    }
