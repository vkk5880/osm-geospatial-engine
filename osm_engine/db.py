import sqlite3


def init_single_database(db_path: str, is_spatial: bool):
    """Initializes schema and performance pragmas on a single database."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.isolation_level = None  # Autocommit mode
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.fetchall()
    cur.execute("PRAGMA synchronous = OFF;")
    cur.fetchall()
    cur.execute("PRAGMA cache_size = -64000;")  # 64MB working cache
    cur.fetchall()
    cur.execute("PRAGMA temp_store = MEMORY;")
    cur.fetchall()

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
    cur.close()
    conn.close()


def init_country_databases(raw_db_path: str, spatial_db_path: str):
    """Initializes schema and optimizes performance pragmas for write-heavy loads."""
    init_single_database(raw_db_path, is_spatial=False)
    init_single_database(spatial_db_path, is_spatial=True)


def create_single_db_indexes(db_path: str, is_spatial: bool):
    """Generates secondary B-Tree indexes on a single database and finalizes WAL."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.isolation_level = None
    cur = conn.cursor()
    if not is_spatial:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_qid ON osm_entities(wikidata_qid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_gid ON osm_entities(geoname_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_cc ON osm_entities(country_code);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_h_parent ON osm_hierarchy(parent_osm_type, parent_osm_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_h_child ON osm_hierarchy(child_osm_type, child_osm_id);")
    else:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_gid ON spatial_locations(geoname_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_qid ON spatial_locations(wikidata_qid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_cc ON spatial_locations(country_code);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_admin ON spatial_locations(admin_level);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_coords ON spatial_locations(latitude, longitude);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_h_parent ON spatial_hierarchy(parent_osm_type, parent_osm_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_s_h_child ON spatial_hierarchy(child_osm_type, child_osm_id);")

    cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    cur.fetchall()
    cur.execute("PRAGMA journal_mode = DELETE;")
    cur.fetchall()
    cur.execute("PRAGMA optimize;")
    cur.fetchall()
    cur.close()
    conn.close()


def create_db_indexes(raw_db_path: str, spatial_db_path: str):
    """Generates secondary B-Tree indexes post-ingestion and truncates WAL for static distribution."""
    create_single_db_indexes(raw_db_path, is_spatial=False)
    create_single_db_indexes(spatial_db_path, is_spatial=True)
