import argparse
import sys
import os
import json
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Optional, Dict, Any, List, Set
from osm_engine.config import EXPORT_DIR, SOLO_THRESHOLD_MB
from osm_engine.catalog import (
    load_geonames_country_languages,
    get_geofabrik_targets,
    get_existing_release_assets
)


def _worker_task(payload: tuple) -> Dict[str, Any]:
    """Top-level worker function for multi-processing execution."""
    from osm_engine.pipeline import process_country_target
    c_info, dynamic_languages = payload
    return process_country_target(c_info, dynamic_languages)


def create_combined_archives(continent: Optional[str] = None):
    """Creates combined regional archives for fast bulk access."""
    label = continent or "global"
    if not os.path.exists(EXPORT_DIR):
        return

    spatial_files = [f for f in os.listdir(EXPORT_DIR) if f.endswith("_spatial.sqlite")]
    if spatial_files:
        combined_spatial_zip = os.path.join(EXPORT_DIR, f"{label}_all_spatial.zip")
        with zipfile.ZipFile(combined_spatial_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for sf in sorted(spatial_files):
                zf.write(os.path.join(EXPORT_DIR, sf), arcname=sf)
        spatial_mb = round(os.path.getsize(combined_spatial_zip) / (1024 * 1024), 2)
        print(f"📦 Created combined spatial bundle: {os.path.basename(combined_spatial_zip)} ({spatial_mb} MB, {len(spatial_files)} countries)")


def plan_matrix(targets: List[Dict[str, Any]], max_runners: int = 15, tag_name: str = "release-worldwide", countries_per_runner: int = 4):
    """
    Smart matrix planner:
    - Automatically packs ~3 to 4 countries per runner for typical extracts (< 500MB).
    - Prevents wasting runner jobs on single small countries (e.g., 20 countries -> 5 runners, 11 countries -> 3 runners).
    - Distributes workloads round-robin to balance total download size across runners.
    - Capped at max_runners (default: 15, max: 20).
    """
    if not targets:
        print("ℹ️ No pending targets to schedule.")
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("has_targets=false\n")
                f.write(f"tag_name={tag_name}\n")
                f.write("matrix={\"include\":[]}\n")
        return

    # Calculate optimal runner count based on 4 countries per runner
    needed_runners = (len(targets) + countries_per_runner - 1) // countries_per_runner
    num_runners = max(1, min(max_runners, needed_runners))

    batches: List[List[Dict[str, Any]]] = [[] for _ in range(num_runners)]

    # Sort descending by size to balance total MB evenly across runners
    sorted_targets = sorted(targets, key=lambda x: x.get("size_mb", 0), reverse=True)

    for idx, t in enumerate(sorted_targets):
        batches[idx % num_runners].append(t)

    matrix_include = []
    for b_idx, batch_items in enumerate(batches, start=1):
        if batch_items:
            slug_list = [t["slug"] for t in batch_items]
            total_mb = sum(t.get("size_mb", 0) for t in batch_items)
            matrix_include.append({
                "batch_id": b_idx,
                "countries": ",".join(slug_list),
                "count": len(slug_list),
                "est_mb": round(total_mb, 1)
            })

    matrix_json = json.dumps({"include": matrix_include})
    print(f"\n📊 Smart Matrix Plan Generated: {len(matrix_include)} parallel runner jobs for {len(targets)} total countries (Max Runners: {max_runners}).")
    for m in matrix_include:
        print(f"  • Runner {m['batch_id']}: {m['count']} countries (~{m['est_mb']} MB) -> [{m['countries']}]")

    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("has_targets=true\n")
            f.write(f"tag_name={tag_name}\n")
            f.write(f"matrix={matrix_json}\n")


def main():
    parser = argparse.ArgumentParser(description="High-Throughput OpenStreetMap Geospatial Harvester")
    parser.add_argument("--continent", type=str, default="all",
                        help="Continent name (e.g. central-america, europe, asia, or 'all' for worldwide)")
    parser.add_argument("--country", type=str, help="Specific Geofabrik country slug (e.g. guatemala, monaco)")
    parser.add_argument("--batch", type=str, help="Comma-separated list of country slugs for matrix runner execution")
    parser.add_argument("--workers", type=int, default=2,
                        help="Number of local parallel worker processes (default: 2 for 2-vCPU runners)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-harvesting of all countries, ignoring existing releases")
    parser.add_argument("--repo", type=str, default=os.environ.get("GITHUB_REPOSITORY"),
                        help="GitHub repository (owner/repo) to check existing releases for incremental runs")
    parser.add_argument("--plan-matrix", action="store_true",
                        help="Generate GitHub Actions matrix JSON without executing harvest")
    parser.add_argument("--max-runners", type=int, default=15,
                        help="Maximum parallel runner jobs for matrix planning (default: 15, max 20)")
    parser.add_argument("--countries-per-runner", type=int, default=4,
                        help="Target number of countries per parallel runner (default: 4)")
    args = parser.parse_args()

    print("Fetching Geofabrik targets and GeoNames language maps...")
    dynamic_languages = load_geonames_country_languages()
    targets = get_geofabrik_targets(continent_filter=args.continent)

    # 1. Filter by specific country slug or batch list
    if args.country:
        targets = [t for t in targets if t["slug"].lower() == args.country.lower()]
    elif args.batch:
        batch_slugs = {s.strip().lower() for s in args.batch.split(",") if s.strip()}
        targets = [t for t in targets if t["slug"].lower() in batch_slugs]

    if not targets:
        print("❌ No matching targets found.")
        if args.plan_matrix and "GITHUB_OUTPUT" in os.environ:
            tag_name = f"release-{args.continent.lower()}" if args.continent and args.continent.lower() not in ("all", "worldwide") else "release-worldwide"
            plan_matrix([], args.max_runners, tag_name)
        sys.exit(0 if args.plan_matrix else 1)

    tag_name = f"release-{args.continent.lower()}" if args.continent and args.continent.lower() not in ("all", "worldwide") else "release-worldwide"

    # 2. Incremental / Skip-Existing Resolution
    if not args.force:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        existing_assets = get_existing_release_assets(args.repo, tag_name, token=token) if args.repo else set()
        local_exports = set(os.listdir(EXPORT_DIR)) if os.path.exists(EXPORT_DIR) else set()

        pending_targets = []
        skipped_count = 0
        for t in targets:
            cc = t["primary_cc"]
            slug = t["slug"]
            prefix = cc if len(t["all_ccs"]) == 1 else f"{cc}_{slug}"
            # Check if this target's database or filtered PBF is already published/exported
            is_published = (
                f"{prefix}_spatial.sqlite" in existing_assets or
                f"{prefix}_osm.sqlite" in existing_assets or
                f"{prefix}_filtered.osm.pbf" in existing_assets or
                f"{prefix}_bundle.zip" in existing_assets or
                f"{prefix}_spatial.sqlite" in local_exports or
                f"{prefix}_osm.sqlite" in local_exports or
                f"{prefix}_filtered.osm.pbf" in local_exports
            )
            if is_published:
                print(f"⏭️ Skipping [{prefix}] {t['name']} (already published in {tag_name})")
                skipped_count += 1
            else:
                pending_targets.append(t)

        if skipped_count > 0:
            print(f"⏩ Skipped {skipped_count} existing countries. {len(pending_targets)} targets remaining to process.")
        targets = pending_targets

    # 3. If running matrix planning, output JSON and exit
    if args.plan_matrix:
        plan_matrix(targets, args.max_runners, tag_name, countries_per_runner=args.countries_per_runner)
        sys.exit(0)

    if not targets:
        print("✅ All queued country extracts are already published and up to date!")
        create_combined_archives(continent=args.continent)
        sys.exit(0)

    print(f"🎯 Total targets queued: {len(targets)} (Local Workers: {args.workers})")
    from osm_engine.pipeline import process_country_target

    # Separate targets into SOLO (> SOLO_THRESHOLD_MB) and BATCHABLE
    solo_targets = [t for t in targets if t["size_mb"] >= SOLO_THRESHOLD_MB]
    batch_targets = [t for t in targets if t["size_mb"] < SOLO_THRESHOLD_MB]

    successful_count = 0

    # 1. Process batchable targets in parallel
    if batch_targets:
        if args.workers > 1:
            print(f"\n🚀 Running {len(batch_targets)} standard targets with {args.workers} local workers...")
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                future_to_target = {
                    executor.submit(_worker_task, (t, dynamic_languages)): t
                    for t in batch_targets
                }
                for future in as_completed(future_to_target):
                    t = future_to_target[future]
                    try:
                        stats = future.result()
                        successful_count += 1
                        print(f"✅ Finished [{stats['cc']}] {stats['name']} in {stats['duration_sec']}s: "
                              f"{stats['entities']:,} entities, {stats['polygons']:,} polygons | PBF: {stats['filtered_pbf_mb']} MB")
                    except Exception as err:
                        import traceback
                        print(f"❌ Error processing {t['slug']}: {err}\n{traceback.format_exc()}", file=sys.stderr)
        else:
            for t in batch_targets:
                print(f"\nProcessing [{t['primary_cc']}] {t['name']} ({t['size_mb']} MB)...")
                try:
                    stats = process_country_target(t, dynamic_languages)
                    successful_count += 1
                    print(f"✅ Finished [{stats['cc']}] in {stats['duration_sec']}s: "
                          f"{stats['entities']:,} entities, {stats['polygons']:,} polygons | PBF: {stats['filtered_pbf_mb']} MB")
                except Exception as err:
                    import traceback
                    print(f"❌ Error processing {t['slug']}: {err}\n{traceback.format_exc()}", file=sys.stderr)

    # 2. Process large extracts solo to prevent OOM
    if solo_targets:
        print(f"\n⚖️ Running {len(solo_targets)} large extracts sequentially (SOLO mode to fit runner RAM)...")
        for t in solo_targets:
            print(f"\nProcessing (SOLO) [{t['primary_cc']}] {t['name']} ({t['size_mb']} MB)...")
            try:
                stats = process_country_target(t, dynamic_languages)
                successful_count += 1
                print(f"✅ Finished [{stats['cc']}] in {stats['duration_sec']}s: "
                      f"{stats['entities']:,} entities, {stats['polygons']:,} polygons | PBF: {stats['filtered_pbf_mb']} MB")
            except Exception as err:
                import traceback
                print(f"❌ Error processing {t['slug']}: {err}\n{traceback.format_exc()}", file=sys.stderr)

    if successful_count > 0 and not args.batch:
        print("\n📦 Generating combined regional bundles...")
        create_combined_archives(continent=args.continent)


if __name__ == "__main__":
    main()
