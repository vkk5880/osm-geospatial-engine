import os
from typing import Set, Dict

# Scratch storage: defaults to runner temp or Linux /tmp
BASE_SCRATCH_DIR = os.environ.get("RUNNER_TEMP", "/tmp/osm_scratch")
RAM_TEMP_DIR = os.path.join(BASE_SCRATCH_DIR, "downloads")
RAM_DB_DIR = os.path.join(BASE_SCRATCH_DIR, "databases")
EXPORT_DIR = os.path.join(BASE_SCRATCH_DIR, "exports")

# Ensure required runtime folders exist
for folder in (RAM_TEMP_DIR, RAM_DB_DIR, EXPORT_DIR):
    os.makedirs(folder, exist_ok=True)

# Processing Parameters
ENABLE_PBF_COMPRESSION: bool = True
SOLO_THRESHOLD_MB: int = 1024  # Fits safely inside GitHub 7GB runner limits

# OpenStreetMap Extraction Whitelist
SETTLEMENT_PLACES: Set[str] = {
    "city", "town", "village", "hamlet", "suburb", "municipality"
}
TOP_LANGS: Set[str] = {
    "en", "hi", "es", "fr", "ar", "zh", "ru", "pt", "de", "ja"
}
OSM_TYPE_MAP: Dict[str, str] = {
    "n": "node",
    "w": "way",
    "r": "relation"
}

