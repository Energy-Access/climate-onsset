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

## Cached hazards
Copy `drought_hazard.csv` and `heatwave_hazard.csv` into `onsset/climate_calculations/precomputed_hazards/` to run without raw climate data. Delete them from `precomputed_hazards/` to force recomputation.

## To run
Open `2. OnSSET_Scenarios_MultipleTimeSteps.ipynb`, edit the User-editable paths
cell to point at the files above, set `prio_choice = 6`, run all.
