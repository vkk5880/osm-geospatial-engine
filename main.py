import argparse
import sys
from osm_engine.catalog import load_geonames_country_languages, get_geofabrik_targets
from osm_engine.pipeline import process_country_target


def main():
    parser = argparse.ArgumentParser(description="High-Throughput OpenStreetMap Geospatial Harvester")
    parser.add_argument("--continent", type=str, help="Continent name (e.g. central-america, europe, asia)")
    parser.add_argument("--country", type=str, help="Specific Geofabrik country slug (e.g. guatemala, monaco)")
    args = parser.parse_args()

    print("Fetching Geofabrik targets and GeoNames language maps...")
    dynamic_languages = load_geonames_country_languages()
    targets = get_geofabrik_targets(continent_filter=args.continent)

    if args.country:
        targets = [t for t in targets if t["slug"].lower() == args.country.lower()]

    if not targets:
        print("❌ No matching targets found.")
        sys.exit(1)

    print(f"🎯 Total targets queued: {len(targets)}")
    for t in targets:
        print(f"\nProcessing [{t['primary_cc']}] {t['name']} ({t['size_mb']} MB)...")
        try:
            stats = process_country_target(t, dynamic_languages)
            print(f"✅ Finished {stats['cc']} in {stats['duration_sec']}s: "
                  f"{stats['entities']:,} entities, {stats['polygons']:,} polygons.")
        except Exception as err:
            print(f"❌ Error processing {t['slug']}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()

