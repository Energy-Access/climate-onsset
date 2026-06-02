# Climate-OnSSET

A climate-risk-aware extension of OnSSET. Standard OnSSET picks which settlements to electrify
based on cost and reachability. This fork adds an alternative prioritization that ranks
settlements by **climate risk × vulnerability**, so areas already exposed to hazards and
underserved by infrastructure are prioritized rather than passed over.

See [the main OnSSET README](README.md) for the underlying model. This document covers only
the climate-specific extension.

## Quick start

A fully runnable Niger example lives under
[`climate_example_workflow_niger/`](climate_example_workflow_niger/). Open
`2. OnSSET_Scenarios_MultipleTimeSteps.ipynb`, leave the default paths in the "User-editable
paths" cell, ensure `prio_choice = 6`, move "drought_hazard.csv" and "heatwave_hazard.csv" to the "C:\Users\sondrmsk\Code\ClimateOnSSET\onsset\climate_calculations\precomputed_hazards" folder and run all. Cached per-hazard outputs ship with the repo, so no raw climate data is required for the example.

## What it adds

A new prioritization mode:

```
ClimatePriority = ClimateHazard × ClimateVulnerability
```

where

```
ClimateHazard         = compound score across hazard modules (heatwave, drought, ...)
ClimateVulnerability  = ((1 − NormalizedRelativeWealth) + NormalizedTravelHours) / 2
```

Select it by setting `prio_choice = 6` in the scenarios notebook. Standard OnSSET prio
choices (1–4) still work unchanged.

## Required inputs (for `prio_choice = 6`)

**Settlements CSV** (in addition to the standard OnSSET columns):

| Column                     | Range  | Meaning                                    |
|----------------------------|--------|--------------------------------------------|
| `NormalizedRelativeWealth` | [0, 1] | Higher = wealthier; inverted internally    |
| `NormalizedTravelHours`    | [0, 1] | Higher = more remote; used directly        |

If either column is missing, the run raises `ValueError` by design. Pass
`allow_neutral_vulnerability=True` to `process_climate_data` to fall back to a flat 0.5
vulnerability — collapses priority to "proportional to hazard" and is for exploratory runs
only.

### Producing the vulnerability columns

Both columns express relative position within the country, scaled to [0, 1]. A typical recipe:

1. **Wealth source.** [Relative Wealth Index](https://data.humdata.org/dataset/relative-wealth-index) from HDX (Meta's RWI estimates, ~2.4 km grid). Sample at each settlement's lat/lon centroid.

2. **Travel source.** OnSSET's standard `TravelHours` column is the source — no external sampling needed. Just normalize it to [0, 1] per country (min-max or percentile rank) and write the result back.

3. **Normalize to [0, 1] per country.** Min-max scaling is the simplest:
Percentile rank works equally well.

4. **Write both columns into the settlements CSV** before running `prio_choice = 6`.

**Admin-3 shapefile** (e.g., GADM level 3) — used for the spatial join from settlements to
admin-3 regions.

**Climate data** — either:

- A folder of raw CSVs matching each hazard module's `REQUIRED_FILE_GLOBS` (e.g., daily
  temperature files `*t2m*daily*.csv` for heatwave), used the first time hazards are
  computed. **Or:**
- Pre-computed per-hazard CSVs in `onsset/climate_calculations/precomputed_hazards/`. With
  these in place, the raw data folder is not needed and `climate_folder` can stay `None`.

## How the pipeline runs

`onsset.climate_algorithm.process_climate_data` orchestrates the following:

1. **Discover hazard modules** under `onsset/climate_calculations/` (any module exposing
   `HAZARD_NAME`, `load_input`, and `calculate_hazard`).
2. **For each hazard:** check whether a cached `<hazard>_hazard.csv` exists in
   `precomputed_hazards_folder`. If yes, load it. If no, call `module.load_input(loader)` to
   materialize the raw data, then `module.calculate_hazard(input_data, ...)` to compute the
   per-admin-3 score, and save the result to the cache folder.
3. **Compound** the per-hazard scores into a single `compound_hazard` per admin-3
   (weighted mean if weights are configured, otherwise simple mean).
4. **Map to settlements** via spatial join: each settlement inherits its admin-3's compound
   hazard, then `ClimatePriority = Hazard × Vulnerability` is computed per settlement.

`prio_choice = 6` then consumes the `ClimatePriority` column in the rollout step,
prioritizing settlements with the highest scores.

## Cached hazards

The notebooks default to `climate_folder = None` and load cached per-hazard CSVs from
`onsset/climate_calculations/precomputed_hazards/`. **Cache files have no automatic
invalidation.** If hazard module logic changes (e.g., a different normalization), cached
CSVs become stale silently and every subsequent run uses the old values.

To force recomputation: delete the per-hazard CSVs from `precomputed_hazards/` and set
`climate_folder` to a folder containing raw climate data CSVs. The next run will recompute
and re-cache.

## Writing your own hazard module

A hazard module is a Python file under `onsset/climate_calculations/` that exposes three
top-level names:

```python
HAZARD_NAME = "myhazard"        # lowercase identifier; used for cache filename
REQUIRED_FILE_GLOBS = [...]     # filename patterns of raw inputs the module reads

def load_input(loader):
    """Use the loader to materialize whatever calculate_hazard needs.
    Return a DataFrame (eager) or a callable that yields (filename, DataFrame)
    pairs (streaming). Raise ValueError if required data is unavailable."""

def calculate_hazard(input_data, admin3_gdf, config, detected_columns):
    """Pure transform. Returns a DataFrame with columns
    [admin3_id_col, admin3_name_col, '<HAZARD_NAME>_hazard']
    where the hazard column is in [0, 1]."""
```

`load_input` does the I/O (touching the loader); `calculate_hazard` is a pure DataFrame
transform with no file access. This split makes hazard modules fixture-testable and means
the framework can swap data sources without rewriting hazard logic.

See [`heatwave_calculation.py`](onsset/climate_calculations/heatwave_calculation.py) for a
streaming example and [`drought_calculation.py`](onsset/climate_calculations/drought_calculation.py)
for an eager-load example.

## Where things live

- `onsset/climate_algorithm.py` — framework: discovery, compound, settlement mapping
- `onsset/climate_calculations/*.py` — pluggable hazard modules
- `onsset/climate_calculations/precomputed_hazards/` — cached per-hazard CSVs
- `climate_example_workflow_niger/` — runnable Niger example with all inputs

## Tests

`pytest test/` covers the framework (compound aggregation, settlement mapping, vulnerability
checks, caching, idempotency). Per project convention, hazard modules themselves are
user-extensible and not unit-tested — the cached example dataset is the integration check.
