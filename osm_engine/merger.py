"""
High-Performance Geospatial Dataset Merger.

Consolidates per-country SQLite databases and binary PBF extracts into 3 unified worldwide/regional assets:
1. Unified Spatial Database ({label}_spatial.sqlite -> {label}_unified_spatial.zip)
2. Unified OSM Entity Database ({label}_osm.sqlite -> {label}_unified_osm.zip)
3. Unified Filtered PBF Archive ({label}_unified_pbf.zip)
"""
import os
import sys
import sqlite3
import zipfile
import shutil
from typing import List, Optional
from .db import init_single_database, create_single_db_indexes


def merge_spatial_databases(spatial_db_files: List[str], target_db_path: str) -> str:
    """Merges multiple country spatial SQLite databases into a single unified spatial database."""
    if os.path.exists(target_db_path):
        os.remove(target_db_path)

    init_single_database(target_db_path, is_spatial=True)

    conn = sqlite3.connect(target_db_path, timeout=120.0)
    conn.isolation_level = None  # Autocommit
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.fetchall()
    cur.execute("PRAGMA synchronous = OFF;")
    cur.fetchall()

    for idx, src_path in enumerate(sorted(spatial_db_files), start=1):
        if not os.path.exists(src_path) or os.path.getsize(src_path) == 0:
            continue
        db_alias = f"src_{idx}"
        cur.execute(f"ATTACH DATABASE ? AS {db_alias};", (src_path,))
        cur.execute(f"INSERT OR REPLACE INTO spatial_locations SELECT * FROM {db_alias}.spatial_locations;")
        cur.execute(f"INSERT OR REPLACE INTO spatial_names SELECT * FROM {db_alias}.spatial_names;")
        cur.execute(f"INSERT OR IGNORE INTO spatial_hierarchy SELECT * FROM {db_alias}.spatial_hierarchy;")
        cur.execute(f"DETACH DATABASE {db_alias};")

    cur.close()
    conn.close()

    create_single_db_indexes(target_db_path, is_spatial=True)
    return target_db_path


def merge_osm_databases(osm_db_files: List[str], target_db_path: str) -> str:
    """Merges multiple country OSM catalog SQLite databases into a single unified OSM database."""
    if os.path.exists(target_db_path):
        os.remove(target_db_path)

    init_single_database(target_db_path, is_spatial=False)

    conn = sqlite3.connect(target_db_path, timeout=120.0)
    conn.isolation_level = None
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.fetchall()
    cur.execute("PRAGMA synchronous = OFF;")
    cur.fetchall()

    for idx, src_path in enumerate(sorted(osm_db_files), start=1):
        if not os.path.exists(src_path) or os.path.getsize(src_path) == 0:
            continue
        db_alias = f"src_{idx}"
        cur.execute(f"ATTACH DATABASE ? AS {db_alias};", (src_path,))
        cur.execute(f"INSERT OR REPLACE INTO osm_entities SELECT * FROM {db_alias}.osm_entities;")
        cur.execute(f"INSERT OR REPLACE INTO localized_names SELECT * FROM {db_alias}.localized_names;")
        cur.execute(f"INSERT OR IGNORE INTO osm_hierarchy SELECT * FROM {db_alias}.osm_hierarchy;")
        cur.execute(f"DETACH DATABASE {db_alias};")

    cur.close()
    conn.close()

    create_single_db_indexes(target_db_path, is_spatial=False)
    return target_db_path


def ensure_max_asset_size(filepath: str, max_bytes: int = 1900 * 1024 * 1024) -> List[str]:
    """Guarantees no release asset exceeds 1.9 GB (1,992,294,400 bytes)."""
    if not os.path.exists(filepath):
        return []
    sz = os.path.getsize(filepath)
    if sz <= max_bytes:
        return [filepath]

    sz_mb = round(sz / (1024 * 1024), 1)
    print(f"⚠️ {os.path.basename(filepath)} ({sz_mb} MB) exceeds 1.9 GB limit. Splitting into chunks...")
    chunks = []
    chunk_idx = 1
    with open(filepath, "rb") as f_in:
        while True:
            block = f_in.read(max_bytes)
            if not block:
                break
            chunk_file = f"{filepath}.{chunk_idx:03d}"
            with open(chunk_file, "wb") as f_out:
                f_out.write(block)
            chunks.append(chunk_file)
            chunk_idx += 1
    os.remove(filepath)
    return chunks


def create_unified_databases(search_dir: str, output_dir: str, label: str = "worldwide"):
    """
    Scans search_dir for country databases and generates the 2 unified SQLite assets:
    1. {label}_unified_spatial.zip (Contains {label}_spatial.sqlite)
    2. {label}_unified_osm.zip (Contains {label}_osm.sqlite)
    Enforces a strict 1.9 GB maximum limit per asset.
    """
    os.makedirs(output_dir, exist_ok=True)
    all_files = [os.path.join(search_dir, f) for f in os.listdir(search_dir)]

    spatial_files = [f for f in all_files if f.endswith("_spatial.sqlite") and "unified" not in f]
    osm_files = [f for f in all_files if f.endswith("_osm.sqlite") and "unified" not in f]

    print(f"\n📊 Consolidating {len(spatial_files)} spatial DBs and {len(osm_files)} OSM DBs into unified release assets...")

    # 1. Merge Spatial DBs
    if spatial_files:
        merged_spatial_db = os.path.join(output_dir, f"{label}_spatial.sqlite")
        print(f"🔄 Merging {len(spatial_files)} spatial databases into {os.path.basename(merged_spatial_db)}...")
        merge_spatial_databases(spatial_files, merged_spatial_db)
        spatial_zip = os.path.join(output_dir, f"{label}_unified_spatial.zip")
        with zipfile.ZipFile(spatial_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(merged_spatial_db, arcname=os.path.basename(merged_spatial_db))
        os.remove(merged_spatial_db)
        ensure_max_asset_size(spatial_zip, max_bytes=1900 * 1024 * 1024)
        sz_mb = round(os.path.getsize(spatial_zip) / (1024 * 1024), 2) if os.path.exists(spatial_zip) else "multi-part"
        print(f"✅ Generated 1/2: {os.path.basename(spatial_zip)} ({sz_mb} MB)")

    # 2. Merge OSM DBs
    if osm_files:
        merged_osm_db = os.path.join(output_dir, f"{label}_osm.sqlite")
        print(f"🔄 Merging {len(osm_files)} OSM databases into {os.path.basename(merged_osm_db)}...")
        merge_osm_databases(osm_files, merged_osm_db)
        osm_zip = os.path.join(output_dir, f"{label}_unified_osm.zip")
        with zipfile.ZipFile(osm_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(merged_osm_db, arcname=os.path.basename(merged_osm_db))
        os.remove(merged_osm_db)
        ensure_max_asset_size(osm_zip, max_bytes=1900 * 1024 * 1024)
        sz_mb = round(os.path.getsize(osm_zip) / (1024 * 1024), 2) if os.path.exists(osm_zip) else "multi-part"
        print(f"✅ Generated 2/2: {os.path.basename(osm_zip)} ({sz_mb} MB)")


create_unified_3files = create_unified_databases



