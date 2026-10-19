import sqlite3


def init_country_databases(raw_db_path: str, spatial_db_path: str):
    """Initializes schema and optimizes performance pragmas for write-heavy loads."""
    for db_path, is_spatial in ((raw_db_path, False), (spatial_db_path, True)):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # High-throughput tuning
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = OFF;")
        cur.execute("PRAGMA cache_size = -64000;")  # 64MB working cache
        cur.execute("PRAGMA temp_store = MEMORY;")

        if not is_spatial:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS osm_entities (
                osm_type TEXT,
                osm_id INTEGER,
                geoname_id INTEGER,
                wikidata_qid TEXT,
                name TEXT,
                country_code TEXT,
                latitude REAL,
                longitude REAL,
                place_type TEXT,
                admin_level INTEGER,
                population INTEGER,
                elevation REAL,
                postal_code TEXT,
                timezone TEXT,
                wikipedia TEXT,
                PRIMARY KEY (osm_type, osm_id)
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS localized_names (
                osm_type TEXT,
                osm_id INTEGER,
                lang_code TEXT,
                name_type TEXT,
                translated_name TEXT,
                PRIMARY KEY (osm_type, osm_id, lang_code, name_type)
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS osm_hierarchy (
                parent_osm_type TEXT,
                parent_osm_id INTEGER,
                child_osm_type TEXT,
                child_osm_id INTEGER,
                role TEXT,
                PRIMARY KEY (parent_osm_type, parent_osm_id, child_osm_type, child_osm_id, role)
            );
            """)
        else:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS spatial_locations (
                osm_type TEXT,
                osm_id INTEGER,
                geoname_id INTEGER,
                wikidata_qid TEXT,
                name TEXT,
                country_code TEXT,
                place_type TEXT,
                admin_level INTEGER,
                latitude REAL,
                longitude REAL,
                bbox_min_lat REAL,
                bbox_min_lon REAL,
                bbox_max_lat REAL,
                bbox_max_lon REAL,
                polygon_geojson TEXT,
                population INTEGER,
                elevation REAL,
                timezone TEXT,
                PRIMARY KEY (osm_type, osm_id)
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS spatial_names (
                osm_type TEXT,
                osm_id INTEGER,
                lang_code TEXT,
                name_type TEXT,
                translated_name TEXT,
                PRIMARY KEY (osm_type, osm_id, lang_code, name_type)
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS spatial_hierarchy (
                parent_osm_type TEXT,
                parent_osm_id INTEGER,
                child_osm_type TEXT,
                child_osm_id INTEGER,
                role TEXT,
                PRIMARY KEY (parent_osm_type, parent_osm_id, child_osm_type, child_osm_id, role)
            );
            """)
        conn.commit()
        conn.close()


def create_db_indexes(raw_db_path: str, spatial_db_path: str):
    """Generates secondary B-Tree indexes post-ingestion and truncates WAL for static distribution."""
    conn_raw = sqlite3.connect(raw_db_path)
    cur_raw = conn_raw.cursor()
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_qid ON osm_entities(wikidata_qid);")
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_gid ON osm_entities(geoname_id);")
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_h_parent ON osm_hierarchy(parent_osm_type, parent_osm_id);")
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_h_child ON osm_hierarchy(child_osm_type, child_osm_id);")

    conn_s = sqlite3.connect(spatial_db_path)
    cur_s = conn_s.cursor()
    cur_s.execute("CREATE INDEX IF NOT EXISTS idx_s_gid ON spatial_locations(geoname_id);")
    cur_s.execute("CREATE INDEX IF NOT EXISTS idx_s_qid ON spatial_locations(wikidata_qid);")
    cur_s.execute("CREATE INDEX IF NOT EXISTS idx_s_admin ON spatial_locations(admin_level);")
    cur_s.execute("CREATE INDEX IF NOT EXISTS idx_s_coords ON spatial_locations(latitude, longitude);")
    cur_s.execute("CREATE INDEX IF NOT EXISTS idx_s_h_parent ON spatial_hierarchy(parent_osm_type, parent_osm_id);")
    cur_s.execute("CREATE INDEX IF NOT EXISTS idx_s_h_child ON spatial_hierarchy(child_osm_type, child_osm_id);")

    # Flush WAL into main database and switch to clean DELETE mode for read-only VFS / HTTP range distribution
    for conn in (conn_raw, conn_s):
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA optimize;")
        conn.close()
