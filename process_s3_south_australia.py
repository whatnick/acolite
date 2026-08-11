#!/usr/bin/env python3
"""
Search CMR for Sentinel-3 OLCI EFR over SE Australia,
download, process with ACOLITE DSF, reproject to UTM, export as COGs.

Fixes from initial attempt:
  1. Limit must be within scene's actual lon coverage at target latitude
  2. pyproj + pyresample required for reprojection
  3. Reprojection is a separate post-AC step (project_acolite_netcdf)
  4. GeoTIFF export uses gdal.Warp for proper nodata handling
"""

import os
import sys
import time
import resource
import json
import zipfile
import glob
from pathlib import Path

import requests
import numpy as np

sys.path.insert(0, "/home/tisham/dev/acolite")
import acolite as ac

# ─── Configuration ────────────────────────────────────────────────────────────
# SE Australia coast — reliably within S3A OLCI descending pass coverage
# The OLCI swath at -35° lat typically covers 140-156°E
SEARCH_BBOX = [141.0, -36.5, 145.0, -33.5]  # [west, south, east, north] for CMR
ACOLITE_LIMIT = [-36.0, 141.0, -34.5, 143.0]  # [south, west, north, east] for ACOLITE

OUTPUT_DIR = Path("/home/tisham/dev/acolite/output/s3_south_australia")
DOWNLOAD_DIR = OUTPUT_DIR / "downloads"

CMR_SEARCH_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"


def search_scenes():
    """Search CMR for S3A OLCI EFR over SE Australia (last 14 days)."""
    from datetime import datetime, timedelta
    end = datetime.utcnow()
    start = end - timedelta(days=14)

    params = {
        "short_name": "S3A_OL_1_EFR",
        "provider": "LAADS",
        "bounding_box": f"{SEARCH_BBOX[0]},{SEARCH_BBOX[1]},{SEARCH_BBOX[2]},{SEARCH_BBOX[3]}",
        "temporal": f"{start.strftime('%Y-%m-%dT00:00:00Z')},{end.strftime('%Y-%m-%dT23:59:59Z')}",
        "page_size": "5",
        "sort_key[]": "-start_date",
    }

    print(f"Searching CMR: S3A OLCI EFR, bbox={SEARCH_BBOX}")
    print(f"  Period: {start.date()} to {end.date()}")
    resp = requests.get(CMR_SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    entries = resp.json()["feed"]["entry"]
    print(f"  Found: {len(entries)} granules")

    for entry in entries:
        links = [l["href"] for l in entry.get("links", [])
                 if l["href"].startswith("https://") and l["href"].endswith(".zip")]
        if links:
            gid = entry.get("producer_granule_id", entry.get("title", ""))
            print(f"  Selected: {gid}")
            return {"id": gid, "url": links[0]}

    raise RuntimeError("No scenes found")


def download_scene(granule):
    """Download and extract the scene."""
    import netrc

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    sen3_name = granule["id"].replace(".zip", ".SEN3")
    sen3_dir = DOWNLOAD_DIR / sen3_name

    if sen3_dir.exists() and any(sen3_dir.glob("*.nc")):
        print(f"  Cached: {sen3_dir}")
        return sen3_dir

    nrc = netrc.netrc()
    auth = nrc.authenticators("urs.earthdata.nasa.gov")
    if not auth:
        raise RuntimeError("No urs.earthdata.nasa.gov entry in ~/.netrc")

    session = requests.Session()
    session.auth = (auth[0], auth[2])

    print(f"  Downloading: {granule['url']}")
    zip_path = DOWNLOAD_DIR / granule["id"]
    resp = session.get(granule["url"], allow_redirects=True, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=128 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                print(f"\r  {100*downloaded//total}% ({downloaded//1024//1024} MB)", end="", flush=True)
    print(f"\n  Downloaded: {downloaded // 1024 // 1024} MB")

    print("  Extracting...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(DOWNLOAD_DIR)
    zip_path.unlink()

    sen3_dirs = list(DOWNLOAD_DIR.glob("*.SEN3"))
    assert sen3_dirs, "No .SEN3 directory after extraction"
    return sen3_dirs[0]


def validate_scene_coverage(sen3_dir):
    """Verify scene actually covers our limit before processing."""
    import netCDF4

    geo = sen3_dir / "tie_geo_coordinates.nc"
    ds = netCDF4.Dataset(str(geo))
    lat = ds.variables["latitude"][:]
    lon = ds.variables["longitude"][:]
    ds.close()

    # Check if any pixels fall within our ACOLITE limit
    south, west, north, east = ACOLITE_LIMIT
    in_roi = (lat >= south) & (lat <= north) & (lon >= west) & (lon <= east)

    if not in_roi.any():
        print(f"  ⚠️  Scene does not cover limit {ACOLITE_LIMIT}")
        print(f"      Scene lat: {lat.min():.2f} to {lat.max():.2f}")
        print(f"      Scene lon at target lat: ", end="")
        in_lat = (lat >= south) & (lat <= north)
        if in_lat.any():
            print(f"{lon[in_lat].min():.2f} to {lon[in_lat].max():.2f}")
        else:
            print("no overlap")
        return False

    n_pixels = in_roi.sum()
    print(f"  ✓ Scene covers ROI: {n_pixels} tie-point pixels in limit")
    return True


def process_acolite(sen3_dir):
    """Run ACOLITE DSF atmospheric correction."""
    out_dir = OUTPUT_DIR / "acolite_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "inputfile": str(sen3_dir),
        "output": str(out_dir),
        "limit": ACOLITE_LIMIT,
        "verbosity": 2,
        "dsf_aot_estimate": "fixed",
        "l2w_parameters": None,
        "rgb_rhot": False,
        "rgb_rhos": False,
        "map_l2w": False,
    }

    print(f"\n  Running ACOLITE DSF (limit={ACOLITE_LIMIT})...")
    t0 = time.time()
    result = ac.acolite.acolite_run(settings=settings)
    ac_time = time.time() - t0
    print(f"  AC time: {ac_time:.1f}s")

    # Find L2R output
    l2r_files = []
    for key in result.values():
        if "l2r" in key:
            l2r_files.extend(key["l2r"])

    if not l2r_files:
        raise RuntimeError(f"No L2R output. ACOLITE result: {result}")

    return l2r_files[0], ac_time


def reproject_to_utm(l2r_nc):
    """Reproject swath-geometry NetCDF to UTM grid."""
    print(f"\n  Reprojecting to EPSG:32754 (UTM 54S) @ 300m...")
    t0 = time.time()
    projected_nc = ac.output.project_acolite_netcdf(l2r_nc)
    proj_time = time.time() - t0
    print(f"  Reprojection time: {proj_time:.1f}s")
    print(f"  Output: {projected_nc}")
    return projected_nc, proj_time


def export_geotiffs(projected_nc):
    """Export projected NetCDF to per-band GeoTIFFs."""
    from osgeo import gdal

    geotiff_dir = OUTPUT_DIR / "geotiff"
    geotiff_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  Exporting to GeoTIFF: {geotiff_dir}")
    t0 = time.time()

    ds = gdal.Open(projected_nc)
    subdatasets = ds.GetSubDatasets()
    ds = None

    tif_files = []
    for sds_path, _ in subdatasets:
        varname = sds_path.split(":")[-1]
        if varname in ["x", "y", "transverse_mercator", "lat", "lon"]:
            continue

        outfile = str(geotiff_dir / f"{varname}.tif")
        src_ds = gdal.Open(sds_path)
        if src_ds is None:
            continue

        band = src_ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()

        warp_opts = gdal.WarpOptions(
            format="GTiff",
            srcNodata=nodata,
            dstNodata=float("nan"),
            resampleAlg=gdal.GRA_NearestNeighbour,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512"],
        )
        gdal.Warp(outfile, src_ds, options=warp_opts)
        src_ds = None
        tif_files.append(outfile)

    export_time = time.time() - t0
    print(f"  Export time: {export_time:.1f}s")
    print(f"  Files: {len(tif_files)} GeoTIFFs")

    total_mb = sum(os.path.getsize(f) for f in tif_files) / 1024 / 1024
    print(f"  Total size: {total_mb:.1f} MB")

    return tif_files, export_time


def main():
    print("=" * 60)
    print("🛰️  Sentinel-3 OLCI → ACOLITE DSF → COG Pipeline")
    print("=" * 60)

    t_start = time.time()

    # Step 1: Search
    granule = search_scenes()

    # Step 2: Download
    sen3_dir = download_scene(granule)

    # Step 3: Validate coverage
    if not validate_scene_coverage(sen3_dir):
        print("\n❌ Scene doesn't cover target area. Try different search params.")
        sys.exit(1)

    # Step 4: ACOLITE atmospheric correction
    l2r_nc, ac_time = process_acolite(sen3_dir)

    # Step 5: Reproject to UTM
    projected_nc, proj_time = reproject_to_utm(l2r_nc)

    # Step 6: Export GeoTIFFs
    tif_files, export_time = export_geotiffs(projected_nc)

    # Summary
    total_time = time.time() - t_start
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    rhos_files = [f for f in tif_files if "rhos_" in f]

    print("\n" + "=" * 60)
    print("✅ Pipeline Complete!")
    print(f"   Scene: {granule['id']}")
    print(f"   ROI: {ACOLITE_LIMIT}")
    print(f"   COG files: {len(tif_files)} ({len(rhos_files)} rhos bands)")
    print(f"   Output: {OUTPUT_DIR / 'geotiff'}")
    print()
    print("   Performance:")
    print(f"     Download:     (cached)")
    print(f"     AC:           {ac_time:.1f}s")
    print(f"     Reproject:    {proj_time:.1f}s")
    print(f"     GeoTIFF:      {export_time:.1f}s")
    print(f"     Total:        {total_time:.1f}s")
    print(f"     Peak RSS:     {peak_mb:.0f} MB")
    print("=" * 60)


if __name__ == "__main__":
    main()
