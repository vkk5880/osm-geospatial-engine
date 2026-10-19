import argparse
import sys
import os
import zipfile
from typing import Optional
from osm_engine.config import EXPORT_DIR
from osm_engine.catalog import (
    load_geonames_country_languages,
    get_geofabrik_targets,
    get_existing_release_assets
)
from osm_engine.pipeline import process_country_target


def create_combined_archives(continent: Optional[str] = None):
    """Creates combined regional archives for faster bulk access."""
    label = continent or "global"
    if not os.path.exists(EXPORT_DIR):
        return

    weather_files = [f for f in os.listdir(EXPORT_DIR) if f.endswith("_weather.sqlite")]
    if weather_files:
        combined_weather_zip = os.path.join(EXPORT_DIR, f"{label}_all_weather.zip")
        with zipfile.ZipFile(combined_weather_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for wf in sorted(weather_files):
                zf.write(os.path.join(EXPORT_DIR, wf), arcname=wf)
        weather_mb = round(os.path.getsize(combined_weather_zip) / (1024 * 1024), 2)
        print(f"📦 Created combined weather bundle: {os.path.basename(combined_weather_zip)} ({weather_mb} MB, {len(weather_files)} countries)")

    bundle_files = [f for f in os.listdir(EXPORT_DIR) if f.endswith("_bundle.zip") and not f.startswith(label)]
    if bundle_files:
        combined_all_zip = os.path.join(EXPORT_DIR, f"{label}_all_bundle.zip")
        with zipfile.ZipFile(combined_all_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for bf in sorted(bundle_files):
                zf.write(os.path.join(EXPORT_DIR, bf), arcname=bf)
        all_mb = round(os.path.getsize(combined_all_zip) / (1024 * 1024), 2)
        print(f"📦 Created combined master bundle: {os.path.basename(combined_all_zip)} ({all_mb} MB, {len(bundle_files)} countries)")


def main():
    parser = argparse.ArgumentParser(description="High-Throughput OpenStreetMap Geospatial Harvester")
    parser.add_argument("--continent", type=str, help="Continent name (e.g. central-america, europe, asia)")
    parser.add_argument("--country", type=str, help="Specific Geofabrik country slug (e.g. guatemala, monaco)")
    parser.add_argument("--force", action="store_true", help="Force re-harvesting of all countries, ignoring existing releases")
    parser.add_argument("--repo", type=str, default=os.environ.get("GITHUB_REPOSITORY"),
                        help="GitHub repository (owner/repo) to check existing releases for incremental runs")
    args = parser.parse_args()

    print("Fetching Geofabrik targets and GeoNames language maps...")
    dynamic_languages = load_geonames_country_languages()
    targets = get_geofabrik_targets(continent_filter=args.continent)

    if args.country:
        targets = [t for t in targets if t["slug"].lower() == args.country.lower()]

    if not targets:
        print("❌ No matching targets found.")
        sys.exit(1)

    # Incremental / Skip-Existing Resolution
    if not args.force:
        tag = f"release-{args.continent}" if args.continent else "release-global"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        existing_assets = get_existing_release_assets(args.repo, tag, token=token) if args.repo else set()
        local_exports = set(os.listdir(EXPORT_DIR)) if os.path.exists(EXPORT_DIR) else set()

        pending_targets = []
        skipped_count = 0
        for t in targets:
            cc = t["primary_cc"]
            # Check if this country was already published or exported
            is_published = (
                f"{cc}_bundle.zip" in existing_assets or
                f"{cc}_osm.sqlite" in existing_assets or
                f"{cc}_bundle.zip" in local_exports or
                f"{cc}_osm.sqlite" in local_exports
            )
            if is_published:
                print(f"⏭️ Skipping [{cc}] {t['name']} (already published in {tag})")
                skipped_count += 1
            else:
                pending_targets.append(t)

        if skipped_count > 0:
            print(f"⏩ Skipped {skipped_count} existing countries. {len(pending_targets)} targets remaining to process.")
        targets = pending_targets

    if not targets:
        print("✅ All queued country extracts are already published and up to date!")
        # Ensure combined bundles exist even if all are skipped
        create_combined_archives(continent=args.continent)
        sys.exit(0)

    print(f"🎯 Total targets queued: {len(targets)}")
    successful_count = 0
    for t in targets:
        print(f"\nProcessing [{t['primary_cc']}] {t['name']} ({t['size_mb']} MB)...")
        try:
            stats = process_country_target(t, dynamic_languages)
            successful_count += 1
            print(f"✅ Finished {stats['cc']} in {stats['duration_sec']}s: "
                  f"{stats['entities']:,} entities, {stats['polygons']:,} polygons | Bundle: {stats.get('bundle_zip_mb', 0)} MB")
        except Exception as err:
            print(f"❌ Error processing {t['slug']}: {err}", file=sys.stderr)

    if successful_count > 0:
        print("\n📦 Generating combined regional bundles...")
        create_combined_archives(continent=args.continent)


if __name__ == "__main__":
    main()
