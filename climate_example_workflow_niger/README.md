# Niger climate-priority example

Inputs for running OnSSET multi-timestep scenarios with `prio_choice=6`
(climate-based settlement prioritization) on Niger.

## Files
- `GEP-OnSSET_InputFile.csv` — settlements (use this for 1. Calibration; the calibration will return the `OnSSET_InputFile_Calibrated.csv` file)
- `OnSSET_InputFile_Calibrated.csv` — settlements calibrated
- `ne-2-pv.csv` / `ne-2-wind.csv` — hourly renewable resource files
- `niger-electricity-transmission-network.zip` — existing MV lines
- `specs.xlsx` / `specs_cal_1305.xlsx` — technology specs if you want to use the gui_runner rather than the notebooks
- `drought_hazard.csv`, `heatwave_hazard.csv` — pre-computed per-admin-3 climate hazards

Both input files include `NormalizedRelativeWealth` and `NormalizedTravelHours`, which `prio_choice = 6` requires (see the main README).

### Large input files (download separately)
`GEP-OnSSET_InputFile.csv` (141 MB) and `OnSSET_InputFile_Calibrated.csv` (99 MB) exceed GitHub's 100 MB per-file limit and are not tracked in this repo. Download them from the [example-data release](https://github.com/Energy-Access/climate-onsset/releases/tag/niger_example_workflow-data-v0.1) and place them in this folder before running the notebooks.

## Cached hazards
Copy `drought_hazard.csv` and `heatwave_hazard.csv` into `onsset/climate_calculations/precomputed_hazards/` to run without raw climate data. Delete them from `precomputed_hazards/` to force recomputation.

## To run
Open `2. OnSSET_Scenarios_MultipleTimeSteps.ipynb`, edit the User-editable paths
cell to point at the files above, set `prio_choice = 6`, run all.

## Cache freshness

The notebooks default to `climate_folder=None` and load cached per-hazard CSVs from `onsset/climate_calculations/precomputed_hazards/`. Cache files have no automatic invalidation: if hazard module logic changes (e.g., normalization), cached CSVs become stale silently. All subsequent runs use outdated values. To force recomputation, delete the per-hazard CSVs from `precomputed_hazards/` and set `climate_folder` to a folder containing raw climate data CSVs.

## Input structure:

In the notebooks, change the file paths to wherever you want
