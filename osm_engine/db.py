import sqlite3


def init_country_databases(raw_db_path: str, weather_db_path: str):
    """Initializes schema and optimizes performance pragmas for write-heavy loads."""
    for db_path, is_weather in ((raw_db_path, False), (weather_db_path, True)):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # High-throughput tuning
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = OFF;")
        cur.execute("PRAGMA cache_size = -64000;")  # 64MB working cache
        cur.execute("PRAGMA temp_store = MEMORY;")

        if not is_weather:
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
                translated_name TEXT,
                PRIMARY KEY (osm_type, osm_id, lang_code)
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
            CREATE TABLE IF NOT EXISTS weather_locations (
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
            CREATE TABLE IF NOT EXISTS weather_names (
                osm_type TEXT,
                osm_id INTEGER,
                lang_code TEXT,
                translated_name TEXT,
                PRIMARY KEY (osm_type, osm_id, lang_code)
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_hierarchy (
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


def create_db_indexes(raw_db_path: str, weather_db_path: str):
    """Generates secondary B-Tree indexes post-ingestion to avoid write overhead."""
    conn_raw = sqlite3.connect(raw_db_path)
    cur_raw = conn_raw.cursor()
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_qid ON osm_entities(wikidata_qid);")
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_gid ON osm_entities(geoname_id);")
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_h_parent ON osm_hierarchy(parent_osm_type, parent_osm_id);")
    cur_raw.execute("CREATE INDEX IF NOT EXISTS idx_raw_h_child ON osm_hierarchy(child_osm_type, child_osm_id);")
    conn_raw.commit()
    conn_raw.close()

    conn_w = sqlite3.connect(weather_db_path)
    cur_w = conn_w.cursor()
    cur_w.execute("CREATE INDEX IF NOT EXISTS idx_w_gid ON weather_locations(geoname_id);")
    cur_w.execute("CREATE INDEX IF NOT EXISTS idx_w_qid ON weather_locations(wikidata_qid);")
    cur_w.execute("CREATE INDEX IF NOT EXISTS idx_w_admin ON weather_locations(admin_level);")
    cur_w.execute("CREATE INDEX IF NOT EXISTS idx_w_coords ON weather_locations(latitude, longitude);")
    cur_w.execute("CREATE INDEX IF NOT EXISTS idx_w_h_parent ON weather_hierarchy(parent_osm_type, parent_osm_id);")
    cur_w.execute("CREATE INDEX IF NOT EXISTS idx_w_h_child ON weather_hierarchy(child_osm_type, child_osm_id);")
    conn_w.commit()
    conn_w.close()

