#!/usr/bin/env python3
"""
GeoSpatial-Engine Dataset Downloader
Zero-dependency CLI utility to download unified databases or country-level extracts.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

REPO = "vkk5880/osm-geospatial-engine"


def fetch_release_metadata(tag: str = "latest") -> dict:
    url = f"https://api.github.com/repos/{REPO}/releases/{tag}" if tag != "latest" else f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "OSM-Dataset-Downloader", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Fallback to listing releases
            list_url = f"https://api.github.com/repos/{REPO}/releases?per_page=1"
            req_list = urllib.request.Request(list_url, headers={"User-Agent": "OSM-Dataset-Downloader"})
            with urllib.request.urlopen(req_list, timeout=15) as resp_list:
                items = json.loads(resp_list.read().decode("utf-8"))
                if items:
                    return items[0]
        sys.exit(f"❌ Failed to fetch release metadata: HTTP {e.code} - {e.reason}")
    except Exception as err:
        sys.exit(f"❌ Network error while connecting to GitHub: {err}")


def download_file(url: str, dest_path: str, size_bytes: int = 0):
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    if os.path.exists(dest_path):
        if size_bytes > 0 and os.path.getsize(dest_path) == size_bytes:
            print(f"⏭️ {os.path.basename(dest_path)} already exists ({round(size_bytes / (1024*1024), 1)} MB), skipping.")
            return

    filename = os.path.basename(dest_path)
    sz_str = f" ({round(size_bytes / (1024*1024), 1)} MB)" if size_bytes > 0 else ""
    print(f"⬇️ Downloading {filename}{sz_str}...")

    req = urllib.request.Request(url, headers={"User-Agent": "OSM-Dataset-Downloader"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f_out:
            total = int(resp.headers.get("content-length", size_bytes or 0))
            downloaded = 0
            chunk_size = 1024 * 1024  # 1 MB chunk
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = int(downloaded * 100 / total)
                    mb_down = round(downloaded / (1024 * 1024), 1)
                    mb_tot = round(total / (1024 * 1024), 1)
                    sys.stdout.write(f"\r   [{'=' * (pct // 4)}{' ' * (25 - (pct // 4))}] {pct}% ({mb_down}/{mb_tot} MB)")
                    sys.stdout.flush()
        print(f"\n   ✅ Finished: {dest_path}")
    except Exception as err:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        print(f"\n   ❌ Failed to download {filename}: {err}")


def main():
    parser = argparse.ArgumentParser(description="Download GeoSpatial-Engine datasets and extracts directly from GitHub Releases.")
    parser.add_argument("--unified", action="store_true", help="Download the 2 unified global SQLite databases (spatial & osm)")
    parser.add_argument("--country", type=str, help="Download all files for a specific ISO country code (e.g. US, FR, JP, GT)")
    parser.add_argument("--all-spatial", action="store_true", help="Download all country spatial SQLite databases (*_spatial.sqlite)")
    parser.add_argument("--all-osm", action="store_true", help="Download all country OSM metadata SQLite databases (*_osm.sqlite)")
    parser.add_argument("--all-pbf", action="store_true", help="Download all cleaned country PBF extracts (*_filtered.osm.pbf)")
    parser.add_argument("--tag", type=str, default="latest", help="Target release tag name (default: latest)")
    parser.add_argument("--output-dir", type=str, default="./downloads", help="Destination folder (default: ./downloads)")
    parser.add_argument("--list", action="store_true", help="List all available assets in the release without downloading")

    args = parser.parse_args()

    print(f"🔍 Fetching release assets for tag: '{args.tag}' from {REPO}...")
    release = fetch_release_metadata(tag=args.tag)
    tag_name = release.get("tag_name", "unknown")
    assets = release.get("assets", [])

    print(f"📦 Found release '{release.get('name', tag_name)}' ({tag_name}) with {len(assets)} assets.\n")

    if args.list:
        print(f"{'Filename':<45} | {'Size':<10}")
        print("-" * 58)
        for a in sorted(assets, key=lambda x: x["name"]):
            sz_mb = f"{round(a.get('size', 0) / (1024*1024), 1)} MB"
            print(f"{a['name']:<45} | {sz_mb:<10}")
        return

    # Filter targets
    matches = []
    if args.unified:
        matches = [a for a in assets if "_unified_" in a["name"]]
    elif args.country:
        cc = args.country.strip().upper()
        matches = [a for a in assets if a["name"].upper().startswith(f"{cc}_")]
    elif args.all_spatial:
        matches = [a for a in assets if a["name"].endswith("_spatial.sqlite")]
    elif args.all_osm:
        matches = [a for a in assets if a["name"].endswith("_osm.sqlite")]
    elif args.all_pbf:
        matches = [a for a in assets if a["name"].endswith("_filtered.osm.pbf")]
    else:
        # Default: if no flag given, download the 2 unified databases
        print("💡 No specific filter specified. Defaulting to downloading the 2 unified global databases.")
        print("   (Tip: Use --country CC, --all-spatial, --all-pbf, or --help to see all options)\n")
        matches = [a for a in assets if "_unified_" in a["name"]]

    if not matches:
        sys.exit("❌ No assets matched your selection.")

    print(f"🎯 Queued {len(matches)} files for download into: {os.path.abspath(args.output_dir)}\n")
    for a in matches:
        dest = os.path.join(args.output_dir, a["name"])
        download_file(a["browser_download_url"], dest, a.get("size", 0))

    print(f"\n🎉 All downloads completed successfully!")


if __name__ == "__main__":
    main()

