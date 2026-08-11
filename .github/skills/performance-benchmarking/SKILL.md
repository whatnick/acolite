---
name: performance-benchmarking
description: "Set up and run CI performance benchmarks comparing Rust acolite-rs against Python ACOLITE on real satellite data. Use when: adding a new sensor benchmark, debugging performance regressions, configuring benchmark workflows, interpreting speedup/memory results, or fixing CI data download issues."
argument-hint: "Sensor or task (e.g., 'landsat benchmark' or 'fix S3 download auth')"
---

# ACOLITE Performance Benchmarking in CI

## When to Use
- Adding a benchmark for a newly ported sensor
- Debugging benchmark CI failures (auth, data download, GDAL issues)
- Investigating performance regressions (speedup dropped)
- Understanding memory usage differences between Rust and Python
- Setting up badge/markdown reporting for benchmark results

## Architecture

```
.github/workflows/
├── s3-benchmark.yml        # S3 OLCI: LAADS DAAC download (needs EarthData secrets)
├── landsat-benchmark.yml   # Landsat 8: GCS public bucket (no auth needed)
└── rust.yml                # Includes Criterion microbenchmarks

BENCHMARK_RESULTS.md        # Auto-updated by CI with latest numbers
.github/benchmark-badge.json # Shields.io dynamic badge endpoint
RUST_PORT_ROADMAP.md        # Contains rendered badges at top
```

## Benchmark Workflow Pattern

Each sensor benchmark follows this structure:

```yaml
jobs:
  benchmark:
    steps:
      - Build Rust (--features "full-io,gdal-support" --example process_<sensor>)
      - Download scene (public source, cached between runs)
      - Run Python ACOLITE (timed with resource.getrusage)
      - Run Rust binary (timed with /usr/bin/time -v)
      - Compare results (generate report, assert speedup > threshold)
      - Commit updated BENCHMARK_RESULTS.md back to branch
      - Upload artifacts (metrics JSON, logs)
```

## Data Sources

| Sensor | Source | Auth Required | Scene Size |
|--------|--------|---------------|-----------|
| S3 OLCI | LAADS DAAC (NASA) | EarthData secrets | ~900 MB zip |
| Landsat 8 | GCS `gcp-public-data-landsat` | None | ~470 MB (7 bands) |
| Landsat 9 | USGS LandsatLook | EarthData + EROS app | ~600 MB |

### GCS Landsat Notes
- Only has **Collection 1** data (scenes ending `_01_T1` or `_01_T2`)
- Frozen since ~2018 — no new data added
- Use `https://storage.googleapis.com/gcp-public-data-landsat/LC08/01/PPP/RRR/SCENE_ID/`
- Verify scene exists with `curl -sI <url>` before hardcoding

### LAADS DAAC (S3 OLCI) Notes
- Requires EarthData account with OB.DAAC Data Access approved
- Set secrets: `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` on repo
- Setup `.netrc` in workflow: `machine urs.earthdata.nasa.gov login $u password $p`
- Download uses bearer token from `/api/users/token` endpoint

### USGS LandsatLook Notes
- Requires EarthData + "USGS/EROS - EROS Registration System" app authorized
- Complex OAuth redirect chain (LandsatLook → ERS → URS → back)
- Prefer GCS for CI (simpler, no auth)

## Key Implementation Details

### Windowed Reads (Critical for Memory)
When processing a geographic subset, ALWAYS use windowed reads:
```rust
// GOOD: Read only the subset from disk
let (rows, cols, gt, proj, wkt) = read_geotiff_metadata(&path)?;
let window = latlon_limit_to_pixel_subset(gt, rows, cols, limit, wkt);
let band = read_geotiff_band_window(&path, window)?;

// BAD: Load entire scene then crop (wastes 10× memory)
let band = read_geotiff_band(&path)?;  // loads full 7471×7761
band.data = band.data.slice(s![r0..r0+nr, c0..c0+nc]).to_owned();
```

### Timing Extraction
Rust binary prints: `Load: X.XXs, AC: X.XXs, Write: X.XXs, Total: X.XXs`
CI parses with: `grep "Total:" log | grep -oP '[\d.]+(?=s)' | tail -1`

Memory from `/usr/bin/time -v`: `grep "Maximum resident" log | grep -oP '[\d]+'`

### Badge Format (consistent across sensors)
```markdown
![S3 Speedup](https://img.shields.io/badge/S3_OLCI-{N}×_faster-brightgreen)
![Landsat Speedup](https://img.shields.io/badge/Landsat_8/9-{N}×_faster-brightgreen)
![Rust Time](https://img.shields.io/badge/Rust-{T}s-blue)
![Python Time](https://img.shields.io/badge/Python-{T}s-orange)
```

Colors: `brightgreen` (≥5×), `green` (≥3×), `yellow` (≥2×), `red` (<2×)

### Cache Strategy
- Scene cache key: unique per SHA (`key: scene-v4-${{ github.sha }}`)
- Rust build cache: by Cargo.lock hash
- NEVER use `restore-keys` with prefix matching for scene data (poisoned cache risk)
- Validate cached files: check `stat().st_size > 1_000_000` not just existence

## Common CI Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `TIFF signature not found` | Pure-Rust tiff crate can't read BigTIFF | Build with `--features gdal-support` |
| `Band N not found` | Scene not downloaded (cached HTML) | Bust cache, validate file sizes |
| `gdal_array ImportError` | numpy/GDAL ABI mismatch | Install numpy first, then `pip install --no-build-isolation GDAL==` |
| `401 Unauthorized` (LAADS) | Missing EarthData secrets or app auth | Set secrets, authorize OB.DAAC app |
| `Network is unreachable` | Transient IPv6 issue on GH runner | Re-trigger (transient) |
| `Limit out of scene` | Geographic limit doesn't match scene coverage | Check tie-point grid or GeoTransform |
| 12 GB RSS for Rust | Loading full scene before subsetting | Use windowed reads (`read_geotiff_band_window`) |

## Thresholds and Assertions

```python
# Speedup assertion (fail CI if Rust regresses)
assert speedup > 2.0, f"Rust should be >2× faster (got {speedup:.1f}×)"

# Memory (informational, not asserted — varies by scene size)
# S3 OLCI: ~1.5 GB Rust, ~2.5 GB Python
# Landsat (windowed): ~500 MB Rust, ~1.4 GB Python

# Time budgets
MAX_PROCESSING_TIME_S = 600  # 10 min total (download + AC + export)
```

## Adding a New Sensor Benchmark

1. Create `.github/workflows/<sensor>-benchmark.yml` following the pattern above
2. Find a public data source (prefer no-auth: GCS, AWS open data)
3. Write the download step with file-size validation
4. Add Python ACOLITE settings matching the Rust example's parameters
5. Add badges to `RUST_PORT_ROADMAP.md` and `BENCHMARK_RESULTS.md`
6. Test locally first: `cargo run --release --features full-io --example process_<sensor> -- --scene /path --limit s,w,n,e`
