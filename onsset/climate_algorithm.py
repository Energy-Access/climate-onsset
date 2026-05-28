"""Climate risk algorithm for OnSSET prioritization.

This module processes climate data (temperature, drought, etc.) from CSV files,
aggregates it to admin-3 level (municipality), calculates risk scores, and
provides prioritization data for electrification planning.

Data flow:
    1. GUI runner prompts for climate data folder + admin-3 shapefile
    2. ClimateDataLoader reads all CSVs, detects temporal resolution
    3. ClimateAggregator averages data per admin-3 area
    4. ClimateRiskCalculator computes individual and compound risk scores
    5. Results are mapped to population clusters for use in onsset.py

Configuration:
    All thresholds and parameters are read from the 'ClimateData' sheet in the
    specs Excel file. See specs.py for column name constants.
"""

import os
import logging
import importlib
import pkgutil
import fnmatch
from enum import Enum
from typing import List, Dict, Optional, Tuple, Generator, Iterable, Any
from collections import defaultdict

import pandas as pd
import numpy as np
import geopandas as gpd

# Hazard calculations live in onsset/climate_calculations/*.py.
# Modules are auto-discovered at runtime. See discover_hazard_modules().

logging.basicConfig(format='%(asctime)s\t\t%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# HARD-CODED HAZARD CONFIG (optional)
# =============================================================================

# If set to a list, only hazards with matching HAZARD_NAME are run.
# If None, all discovered hazards are run.
ENABLED_HAZARDS: Optional[List[str]] = None

# Optional hardcoded weights per hazard name.
# Precedence: Specs Excel (if provided) > HARD_CODED_WEIGHTS > default(equal).
HARD_CODED_WEIGHTS: Dict[str, float] = {}


# =============================================================================
# CONSTANTS & CONFIGURATION
# =============================================================================

class TemporalResolution(Enum):
    """Detected temporal resolution of climate data."""
    HOURLY = 'hourly'
    DAILY = 'daily'
    MONTHLY = 'monthly'
    YEARLY = 'yearly'
    UNKNOWN = 'unknown'


class ClimateDataType(Enum):
    """Type of climate variable measured."""
    TEMPERATURE = 'temperature'
    PRECIPITATION = 'precipitation'
    PEV = 'pev'  # Potential Evapotranspiration
    UNKNOWN = 'unknown'


def detect_temporal_from_filename(filename: str) -> TemporalResolution:
    """Detect temporal resolution from filename patterns.

    Args:
        filename: Name of the climate data file.

    Returns:
        Detected TemporalResolution enum value.
    """
    fname_lower = filename.lower()
    if 'hourly' in fname_lower:
        return TemporalResolution.HOURLY
    elif 'daily' in fname_lower or 'dailymax' in fname_lower:
        return TemporalResolution.DAILY
    elif 'monthly' in fname_lower or 'monthlytotal' in fname_lower:
        return TemporalResolution.MONTHLY
    elif 'yearly' in fname_lower or 'annual' in fname_lower:
        return TemporalResolution.YEARLY
    return TemporalResolution.UNKNOWN


def detect_datatype_from_filename(filename: str) -> ClimateDataType:
    """Detect climate data type from filename patterns.

    Args:
        filename: Name of the climate data file.

    Returns:
        Detected ClimateDataType enum value.
    """
    fname_lower = filename.lower()
    # Temperature indicators
    if any(pat in fname_lower for pat in ['t2m', 'temp', 'temperature', 'tmax', 'tmin']):
        return ClimateDataType.TEMPERATURE
    # Precipitation indicators
    if any(pat in fname_lower for pat in ['tp_', 'tp-', 'precip', 'precipitation', 'rainfall', 'rain']):
        return ClimateDataType.PRECIPITATION
    # PEV indicators
    if any(pat in fname_lower for pat in ['pev', 'evapotranspiration', 'evap', 'pet']):
        return ClimateDataType.PEV
    return ClimateDataType.UNKNOWN


# Output column names (for integration with onsset.py)
SET_NORMALIZED_CLIMATE_HAZARD = 'NormalizedClimateHazard'  # Compound normalized hazard score
SET_CLIMATE_HAZARD = 'ClimateHazard'  # Explicit hazard component (heatwave + drought)
SET_CLIMATE_VULNERABILITY = 'ClimateVulnerability'  # Susceptibility component (wealth + remoteness)
SET_CLIMATE_PRIORITY = 'ClimatePriority'  # Prioritization score: Hazard × Vulnerability
SET_CLIMATE_RISK_HEATWAVE = 'ClimateRiskHeatwave'  # Individual heatwave hazard
SET_CLIMATE_RISK_DROUGHT = 'ClimateRiskDrought'  # Individual drought hazard
SET_ADMIN3_ID = 'Admin3ID'  # Municipality identifier

# Deprecated: SET_CLIMATE_EXPOSURE no longer used (population accounts for scheduling, not priority)
# Deprecated: SET_CLIMATE_RISK previously stored population-weighted risk; now use ClimatePriority

def _list_input_files(folder_path: str) -> List[str]:
    """List input climate files (CSV/Excel) in a folder."""
    if not os.path.isdir(folder_path):
        raise ValueError(f"Climate data folder not found: {folder_path}")
    files = [
        f for f in os.listdir(folder_path)
        if f.lower().endswith(('.csv', '.xlsx', '.xls'))
    ]
    files.sort()
    return files


def _match_any_glob(filename: str, globs: Iterable[str]) -> bool:
    name = filename.lower()
    for pat in globs:
        if fnmatch.fnmatch(name, str(pat).lower()):
            return True
    return False


def _require_any_file(
    *,
    folder_path: str,
    all_files: List[str],
    hazard_name: str,
    required_globs: List[str],
) -> None:
    """Require at least one file matching ANY of required_globs."""
    if not required_globs:
        return
    if any(_match_any_glob(f, required_globs) for f in all_files):
        return

    # Strict failure with a clear message.
    found_preview = ', '.join(all_files[:25])
    more = '' if len(all_files) <= 25 else f" (+{len(all_files) - 25} more)"
    raise ValueError(
        f"Hazard '{hazard_name}' could not find any required climate file in: {folder_path}. "
        f"Expected at least one file matching any of: {required_globs}. "
        f"Found {len(all_files)} files: {found_preview}{more}"
    )


# =============================================================================
# CONFIGURATION SCHEMA
# =============================================================================
# Per-hazard parameters are owned by each climate_calculations/<hazard>.py
# module via its CONFIG_SCHEMA attribute. Hazard modules are auto-discovered
# (see discover_hazard_modules()). To add a new hazard (e.g. spei):
#   1. Add climate_calculations/spei_calculation.py exposing HAZARD_NAME and calculate_hazard()
#   2. Optionally add CONFIG_SCHEMA/WEIGHT_KEY/HAZARD_LABEL for config + nicer outputs
# Nothing else needs to change.

# Name of the Excel sheet that holds climate config overrides. A hazard module
# may override this per-schema via the 'spec_sheet' key.
CLIMATE_CONFIG_SHEET = 'ClimateData'

# Row index (0-based) within the sheet to read. A hazard module may override
# via the 'spec_row' key on its schema.
CLIMATE_CONFIG_ROW = 0

# Shared parameters used by the loader, spatial join, and any hazard module.
COMMON_CONFIG_SCHEMA = {
    'fields': {
        'lat_column':         ('LatitudeColumnName',  'latitude'),
        'lon_column':         ('LongitudeColumnName', 'longitude'),
        'date_column':        ('DateColumnName',      'date'),
        'admin3_id_column':   ('Admin3IDColumn',      'GID_3'),
        'admin3_name_column': ('Admin3NameColumn',    'NAME_3'),
    },
}

def discover_hazard_modules() -> List[Any]:
    """Discover hazard modules in onsset.climate_calculations.

    A hazard module is considered valid if it exposes:
      - HAZARD_NAME (str)
      - calculate_hazard(loader, admin3_gdf, config, detected_columns) -> DataFrame
    CONFIG_SCHEMA and WEIGHT_KEY are optional.
    """
    pkg = None
    pkg_name = None
    search_paths: List[str]

    # Prefer package-relative import when running as onsset package
    try:
        from . import climate_calculations as pkg  # type: ignore
        pkg_name = f'{__package__}.climate_calculations' if __package__ else 'onsset.climate_calculations'
        search_paths = list(pkg.__path__)
    except Exception:
        # Fallback to absolute import
        try:
            from onsset import climate_calculations as pkg  # type: ignore
            pkg_name = 'onsset.climate_calculations'
            search_paths = list(pkg.__path__)
        except Exception:
            # Last-resort: local import when running from the onsset folder
            from climate_calculations import __path__ as pkg_path  # type: ignore
            pkg = None
            pkg_name = 'climate_calculations'
            search_paths = list(pkg_path)

    modules: List[Any] = []

    for modinfo in pkgutil.iter_modules(search_paths):
        if modinfo.ispkg:
            continue
        if modinfo.name.startswith('_'):
            continue
        full_name = f'{pkg_name}.{modinfo.name}'
        try:
            module = importlib.import_module(full_name)
        except Exception as e:
            logger.warning(f"Failed to import hazard module '{full_name}': {e}")
            continue

        if not hasattr(module, 'HAZARD_NAME') or not hasattr(module, 'calculate_hazard'):
            continue
        modules.append(module)

    # Stable ordering for deterministic results
    modules.sort(key=lambda m: str(getattr(m, 'HAZARD_NAME', '')).lower())
    return modules


def _filter_enabled_hazards(modules: Iterable[Any]) -> List[Any]:
    if ENABLED_HAZARDS is None:
        return list(modules)
    enabled = {h.strip().lower() for h in ENABLED_HAZARDS}
    result = [m for m in modules if str(getattr(m, 'HAZARD_NAME', '')).lower() in enabled]
    missing = enabled - {str(getattr(m, 'HAZARD_NAME', '')).lower() for m in result}
    if missing:
        raise ValueError(f"Enabled hazards not found: {sorted(missing)}")
    return result


def _iter_schemas(hazard_modules: Optional[Iterable[Any]] = None):
    """Yield (schema_dict, sheet_name, row_idx) for common + every hazard schema."""
    yield COMMON_CONFIG_SCHEMA, CLIMATE_CONFIG_SHEET, CLIMATE_CONFIG_ROW
    if hazard_modules is None:
        hazard_modules = _filter_enabled_hazards(discover_hazard_modules())
    for module in hazard_modules:
        schema = getattr(module, 'CONFIG_SCHEMA', None)
        if not schema:
            continue
        yield (
            schema,
            schema.get('spec_sheet', CLIMATE_CONFIG_SHEET),
            schema.get('spec_row', CLIMATE_CONFIG_ROW),
        )


def load_climate_config(specs_path: Optional[str] = None, hazard_modules: Optional[Iterable[Any]] = None) -> Dict:
    """Load climate configuration from specs file or use defaults.

    Iterates the common schema and every registered hazard schema to build the
    merged config dict, then overrides values from the specs Excel file when
    present.

    Args:
        specs_path: Path to specs Excel file. If None, use defaults.

    Returns:
        Dictionary with configuration values.
    """
    # Start from defaults declared in each schema.
    config: Dict = {}
    for schema, _, _ in _iter_schemas(hazard_modules):
        for key, (_, default) in schema['fields'].items():
            config[key] = default

    if specs_path is None or not os.path.exists(specs_path):
        logger.info("Using default climate configuration values")
        return config

    # Group schemas by (sheet, row) so each sheet is read at most once.
    by_sheet: Dict[Tuple[str, int], List[Tuple[str, str]]] = defaultdict(list)
    for schema, sheet, row_idx in _iter_schemas(hazard_modules):
        for key, (spec_col, _) in schema['fields'].items():
            by_sheet[(sheet, row_idx)].append((spec_col, key))

    for (sheet, row_idx), entries in by_sheet.items():
        try:
            sheet_df = pd.read_excel(specs_path, sheet_name=sheet)
            if sheet_df.empty or row_idx >= len(sheet_df):
                logger.warning(f"Sheet '{sheet}' has no row {row_idx}, using defaults for its fields")
                continue
            row = sheet_df.iloc[row_idx]
            for spec_col, config_key in entries:
                if spec_col in row.index and pd.notna(row[spec_col]):
                    config[config_key] = row[spec_col]
        except Exception as e:
            logger.warning(f"Failed to load sheet '{sheet}': {e}. Using defaults for its fields.")

    logger.info("Loaded climate configuration from specs file")
    return config


# =============================================================================
# DATA LOADING
# =============================================================================

class ClimateDataLoader:
    """Strict climate data loader.

    - Classifies files by filename only (strict).
    - Ignores extra/unrecognized files.
    - Loads only the relevant classified files when asked.
    """

    def __init__(self, folder_path: str, config: Dict):
        """
        Args:
            folder_path: Path to folder containing climate CSV files.
            config: Configuration dictionary from load_climate_config().
        """
        self.folder_path = folder_path
        self.config = config

        # classified_files[temporal][datatype] = [filenames]
        self.classified_files: Dict[TemporalResolution, Dict[ClimateDataType, List[str]]] = {
            res: {dtype: [] for dtype in ClimateDataType}
            for res in TemporalResolution
        }
        self._files_classified: bool = False
        # Flag to prevent re-classification

    # -------------------------------------------------------------------------
    # File Classification (strict: filename only)
    # -------------------------------------------------------------------------

    def _classify_files(self) -> None:
        """Classify files by temporal resolution AND data type independently."""
        # Check if already classified to prevent duplicate entries
        if self._files_classified:
            return

        all_files = _list_input_files(self.folder_path)

        if not all_files:
            raise ValueError(f"No CSV or Excel files found in {self.folder_path}")

        for filename in all_files:
            # Independent detection
            temporal = detect_temporal_from_filename(filename)
            datatype = detect_datatype_from_filename(filename)

            # Store in nested structure
            self.classified_files[temporal][datatype].append(filename)

        # Sort all lists for consistent processing order
        for temporal in self.classified_files:
            for datatype in self.classified_files[temporal]:
                self.classified_files[temporal][datatype].sort()

        self._files_classified = True
        self._log_classification_summary()

    def _log_classification_summary(self) -> None:
        """Log summary of file classification."""
        summary_parts = []
        for temporal in TemporalResolution:
            if temporal == TemporalResolution.UNKNOWN:
                continue
            for datatype in ClimateDataType:
                if datatype == ClimateDataType.UNKNOWN:
                    continue
                count = len(self.classified_files[temporal][datatype])
                if count > 0:
                    summary_parts.append(f"{count} {temporal.value} {datatype.value}")

        unknown_count = sum(
            len(self.classified_files[TemporalResolution.UNKNOWN][dt])
            for dt in ClimateDataType
        )
        if unknown_count:
            summary_parts.append(f"{unknown_count} unclassified")

        logger.info(f"Classified files: {', '.join(summary_parts)}")

    # -------------------------------------------------------------------------
    # Data Availability Checks
    # -------------------------------------------------------------------------

    def has_data(self, temporal: TemporalResolution, datatype: ClimateDataType) -> bool:
        """Check if data is available for a specific temporal/datatype combination.

        Args:
            temporal: Temporal resolution to check.
            datatype: Data type to check.

        Returns:
            True if files exist for this combination.
        """
        if not any(
            self.classified_files[t][d]
            for t in TemporalResolution
            for d in ClimateDataType
        ):
            self._classify_files()
        return len(self.classified_files[temporal][datatype]) > 0

    def get_available_combinations(self) -> List[Tuple[TemporalResolution, ClimateDataType]]:
        """Get list of all (temporal, datatype) combinations with available data.

        Returns:
            List of (TemporalResolution, ClimateDataType) tuples with files.
        """
        if not any(
            self.classified_files[t][d]
            for t in TemporalResolution
            for d in ClimateDataType
        ):
            self._classify_files()

        result = []
        for temporal in TemporalResolution:
            for datatype in ClimateDataType:
                if self.classified_files[temporal][datatype]:
                    result.append((temporal, datatype))
        return result

    def has_daily_temp_data(self) -> bool:
        """Check if daily temperature data is available."""
        return self.has_data(TemporalResolution.DAILY, ClimateDataType.TEMPERATURE)

    def has_monthly_precip_data(self) -> bool:
        """Check if monthly precipitation data is available."""
        return self.has_data(TemporalResolution.MONTHLY, ClimateDataType.PRECIPITATION)

    def _load_file(self, filename: str) -> Optional[pd.DataFrame]:
        """Load a single file (CSV or Excel)."""
        filepath = os.path.join(self.folder_path, filename)
        try:
            if filename.lower().endswith('.csv'):
                return pd.read_csv(filepath)
            else:
                return pd.read_excel(filepath)
        except Exception as e:
            logger.warning(f"Failed to load {filename}: {e}")
            return None

    # -------------------------------------------------------------------------
    # Generic Loader Methods
    # -------------------------------------------------------------------------

    def iter_files(
        self,
        temporal: Optional[TemporalResolution] = None,
        datatype: Optional[ClimateDataType] = None
    ) -> Generator[Tuple[str, pd.DataFrame], None, None]:
        """Iterate over classified files, optionally filtered by temporal/datatype.

        Args:
            temporal: Filter by temporal resolution (None = all)
            datatype: Filter by data type (None = all)

        Yields:
            Tuple of (filename, DataFrame) for each matching file.
        """
        if not any(
            self.classified_files[t][d]
            for t in TemporalResolution
            for d in ClimateDataType
        ):
            self._classify_files()

        temporals = [temporal] if temporal else list(TemporalResolution)
        datatypes = [datatype] if datatype else list(ClimateDataType)

        for t in temporals:
            for d in datatypes:
                for filename in self.classified_files[t][d]:
                    df = self._load_file(filename)
                    if df is not None:
                        logger.info(f"Loaded {filename}: {len(df)} rows")
                        yield filename, df

    def load_files(
        self,
        temporal: Optional[TemporalResolution] = None,
        datatype: Optional[ClimateDataType] = None
    ) -> pd.DataFrame:
        """Load and combine files matching temporal/datatype criteria.

        Args:
            temporal: Filter by temporal resolution (None = all)
            datatype: Filter by data type (None = all)

        Returns:
            Combined DataFrame with all matching data.
        """
        dfs = []
        for filename, df in self.iter_files(temporal, datatype):
            dfs.append(df)

        if not dfs:
            return pd.DataFrame()

        combined = pd.concat(dfs, ignore_index=True)

        logger.info(f"Combined data: {len(combined)} total rows")
        return combined

    def load_monthly_precip_files(self) -> pd.DataFrame:
        """Load only monthly precipitation files (memory-efficient for drought analysis).

        Returns:
            Combined DataFrame with monthly precipitation data.
        """
        df = self.load_files(
            temporal=TemporalResolution.MONTHLY,
            datatype=ClimateDataType.PRECIPITATION
        )
        if not df.empty:
            logger.info(f"Combined monthly precip data: {len(df)} total rows")
        else:
            logger.warning("No monthly precipitation files found")
        return df

    def iter_daily_temp_files(self):
        """Iterate over daily temperature files one at a time (memory-efficient).

        Yields:
            Tuple of (filename, DataFrame) for each daily temperature file.
        """
        yield from self.iter_files(
            temporal=TemporalResolution.DAILY,
            datatype=ClimateDataType.TEMPERATURE
        )

    # (Column detection and temporal inference deliberately removed for strictness.)


# =============================================================================
# COMPOUND RISK & SETTLEMENT MAPPING
# =============================================================================

def _coerce_weight(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except Exception:
        return None
    if not np.isfinite(f):
        return None
    return f


def _camelize_label(text: str) -> str:
    """Convert a hazard name/label to a CamelCase column suffix."""
    parts = [p for p in str(text).replace('-', '_').replace(' ', '_').split('_') if p]
    if not parts:
        return ''
    return ''.join(p[:1].upper() + p[1:] for p in parts)


def _hazard_weight(hazard_name: str, module: Any, config: Dict) -> Optional[float]:
    """Resolve a hazard's weight with precedence: specs(config) > hardcoded > None."""
    weight_key = getattr(module, 'WEIGHT_KEY', None)
    if isinstance(weight_key, str) and weight_key:
        cfg_weight = _coerce_weight(config.get(weight_key))
        if cfg_weight is not None:
            return cfg_weight

    hardcoded = _coerce_weight(HARD_CODED_WEIGHTS.get(hazard_name.lower()))
    if hardcoded is not None:
        return hardcoded

    return None


def _normalize_if_needed(values: pd.Series, label: str) -> pd.Series:
    """Ensure hazards are normalized (0..1). If not, min-max normalize and warn."""
    numeric = pd.to_numeric(values, errors='coerce')
    finite = numeric[np.isfinite(numeric)]
    if finite.empty:
        return numeric

    vmin = float(finite.min())
    vmax = float(finite.max())
    if vmin >= 0.0 and vmax <= 1.0:
        return numeric

    if vmax <= vmin:
        logger.warning(f"Hazard '{label}' not normalizable (min==max). Setting to 0.")
        return pd.Series(0.0, index=values.index)

    logger.warning(
        f"Hazard '{label}' appears not normalized (min={vmin:.3g}, max={vmax:.3g}). "
        "Applying min-max normalization as a fallback."
    )
    return (numeric - vmin) / (vmax - vmin)


def calculate_compound_hazard(
    hazard_dfs: List[pd.DataFrame],
    hazard_modules: List[Any],
    config: Dict,
) -> pd.DataFrame:
    """Combine N hazard outputs into one compound hazard score.

    - Each hazard module returns a per-admin3 DataFrame with a single
      normalized column named '<hazard>_hazard'.
    - Missing hazards for an admin3 are ignored (weights renormalized).
    - If any explicit weights exist, weighted mean is used; otherwise simple mean.
    """
    admin3_id_col = config['admin3_id_column']
    admin3_name_col = config['admin3_name_column']

    if not hazard_dfs:
        return pd.DataFrame(columns=[admin3_id_col, admin3_name_col, 'compound_hazard'])

    # Build merged frame of all admin3 IDs
    merged = None
    hazard_cols: List[str] = []
    weight_by_col: Dict[str, Optional[float]] = {}

    for module, df in zip(hazard_modules, hazard_dfs):
        hazard_name = str(getattr(module, 'HAZARD_NAME', '')).strip().lower()
        out_col = str(getattr(module, 'OUTPUT_COLUMN', f'{hazard_name}_hazard'))
        if out_col not in df.columns:
            raise ValueError(f"Hazard module '{hazard_name}' did not return required column '{out_col}'.")

        raw_label = getattr(module, 'HAZARD_LABEL', None)
        label = _camelize_label(raw_label) if raw_label else _camelize_label(hazard_name)
        weight_by_col[out_col] = _hazard_weight(hazard_name, module, config)

        df_small = df[[admin3_id_col, admin3_name_col, out_col]].copy()
        df_small[out_col] = _normalize_if_needed(df_small[out_col], label)

        if merged is None:
            merged = df_small
        else:
            merged = merged.merge(df_small, on=[admin3_id_col, admin3_name_col], how='outer')

        hazard_cols.append(out_col)

    assert merged is not None

    # Decide whether to use weights
    use_weights = any(w is not None for w in weight_by_col.values())
    weights = np.array([
        (weight_by_col[c] if weight_by_col[c] is not None else 1.0)
        for c in hazard_cols
    ], dtype=float)

    values_matrix = merged[hazard_cols].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
    valid_mask = np.isfinite(values_matrix)

    if use_weights:
        weighted_vals = values_matrix * weights
        weighted_vals[~valid_mask] = 0.0
        denom = (valid_mask * weights).sum(axis=1)
        numer = weighted_vals.sum(axis=1)
        compound = np.where(denom > 0, numer / denom, np.nan)
    else:
        # Simple mean of available hazards per admin3
        vals = values_matrix.copy()
        vals[~valid_mask] = np.nan
        compound = np.nanmean(vals, axis=1)

    merged['compound_hazard'] = compound
    return merged


def map_risk_to_settlements(
    settlements_df: pd.DataFrame,
    risk_df: pd.DataFrame,
    admin3_gdf: gpd.GeoDataFrame,
    config: Dict,
    hazard_label_by_name: Optional[Dict[str, str]] = None,
    lat_col: str = 'Y_deg',
    lon_col: str = 'X_deg',
    allow_neutral_vulnerability: bool = False
) -> pd.DataFrame:
    """Map climate risk from admin-3 regions to settlements.

    Computes climate prioritization score: Priority = Hazard × Vulnerability
    Population is NOT included in the priority score because it is already accounted
    for by the electrification rollout rule (fixed % targets per timestep).

    Args:
        settlements_df: OnSSET settlements DataFrame.
        risk_df: DataFrame with risk scores per admin3.
        admin3_gdf: GeoDataFrame with admin-3 boundaries.
        config: Configuration dictionary.
        lat_col: Name of latitude column in settlements.
        lon_col: Name of longitude column in settlements.

    Returns:
        Settlements DataFrame with climate columns added:
        - ClimateHazard: Compound hazard (heatwave + drought)
        - ClimateVulnerability: Susceptibility (wealth + remoteness)
        - ClimatePriority: Final prioritization score (Hazard × Vulnerability)
        - ClimateHazard{HazardName}: Per-hazard normalized hazard (optional, auto)
        - ClimateRiskHeatwave, ClimateRiskDrought: Legacy per-hazard columns
    """
    admin3_id_col = config['admin3_id_column']
    admin3_name_col = config['admin3_name_column']

    # Convert settlements to GeoDataFrame
    gdf_settlements = gpd.GeoDataFrame(
        settlements_df,
        geometry=gpd.points_from_xy(settlements_df[lon_col], settlements_df[lat_col]),
        crs="EPSG:4326"
    )

    # Ensure admin3 is in correct CRS
    if admin3_gdf.crs is None or admin3_gdf.crs.to_epsg() != 4326:
        admin3_gdf = admin3_gdf.to_crs(epsg=4326)

    # Spatial join
    gdf_join = gpd.sjoin(
        gdf_settlements,
        admin3_gdf[[admin3_id_col, 'geometry']],
        how='left',
        predicate='within'
    )

    # Add admin3 ID to settlements
    settlements_df[SET_ADMIN3_ID] = gdf_join[admin3_id_col].values

    # Merge hazard scores
    hazard_cols = [c for c in risk_df.columns if str(c).lower().endswith('_hazard') and c != 'compound_hazard']
    merge_cols = [admin3_id_col]
    if admin3_name_col in risk_df.columns:
        merge_cols.append(admin3_name_col)
    if 'compound_hazard' in risk_df.columns:
        merge_cols.append('compound_hazard')
    merge_cols.extend(hazard_cols)

    settlements_df = settlements_df.merge(
        risk_df[merge_cols],
        left_on=SET_ADMIN3_ID,
        right_on=admin3_id_col,
        how='left'
    )

    # Rename / compute columns for OnSSET integration
    compound_col = 'compound_hazard' if 'compound_hazard' in settlements_df.columns else None
    if compound_col is None and 'compound_risk' in settlements_df.columns:
        compound_col = 'compound_risk'

    if compound_col is not None:
        hazard_values = pd.to_numeric(settlements_df[compound_col], errors='coerce')
        hazard_min = hazard_values.min()
        hazard_max = hazard_values.max()
        if pd.notna(hazard_min) and pd.notna(hazard_max) and hazard_max > hazard_min:
            hazard_values = (hazard_values - hazard_min) / (hazard_max - hazard_min)
        else:
            hazard_values = hazard_values.fillna(0)

        # Store normalized hazard component (keep legacy name for backward compatibility)
        settlements_df[SET_NORMALIZED_CLIMATE_HAZARD] = hazard_values
        settlements_df[SET_CLIMATE_HAZARD] = hazard_values

        # Compute Vulnerability component: ((1 - normalized_wealth) + normalized_travel) / 2
        # Try multiple column name variants for robustness
        wealth_col = next((col for col in [
            'NormalizedRelativeWealth',
            'normalized_wealth_index',
            'NormalizedWealth',
            'normalized_relative_wealth',
        ] if col in settlements_df.columns), None)

        travel_col = next((col for col in [
            'NormalizedTravelHours',
            'normalized_travel_hours',
            'NormalizedTravel',
        ] if col in settlements_df.columns), None)

        if wealth_col and travel_col:
            wealth_values = pd.to_numeric(settlements_df[wealth_col], errors='coerce').fillna(0)
            travel_values = pd.to_numeric(settlements_df[travel_col], errors='coerce').fillna(0)
            # Vulnerability: invert wealth (high wealth = low vulnerability), average with travel
            vulnerability_values = ((1 - wealth_values) + travel_values) / 2
            settlements_df[SET_CLIMATE_VULNERABILITY] = vulnerability_values
        else:
            if not allow_neutral_vulnerability:
                raise ValueError(
                    "Missing required vulnerability columns: NormalizedRelativeWealth, NormalizedTravelHours "
                    "(values in [0, 1]). See README section 'Climate prioritization' for details and aliases. "
                    "Pass allow_neutral_vulnerability=True to use a flat 0.5 fallback."
                )
            # Fallback: if wealth/travel missing, set vulnerability to neutral (0.5)
            vulnerability_values = pd.Series(0.5, index=settlements_df.index)
            logger.warning("Wealth or travel columns not found; using neutral vulnerability value 0.5")
            settlements_df[SET_CLIMATE_VULNERABILITY] = vulnerability_values

        # Compute Climate Priority: Hazard × Vulnerability
        # NOTE: Population is NOT included here. Population affects WHEN targets are reached
        # (cumulative % targets per timestep), not WHO should be prioritized first.
        priority_values = hazard_values * vulnerability_values
        settlements_df[SET_CLIMATE_PRIORITY] = priority_values

    # Auto-create per-hazard settlement columns
    if hazard_label_by_name:
        for hazard_name, label in hazard_label_by_name.items():
            col = f"{hazard_name.lower()}_hazard"
            if col not in settlements_df.columns:
                continue
            settlements_df[f"ClimateHazard{label}"] = pd.to_numeric(settlements_df[col], errors='coerce').fillna(0)

    # Legacy columns for backward compatibility (keep existing names used in exporters/docs)
    if 'heatwave_hazard' in settlements_df.columns:
        settlements_df[SET_CLIMATE_RISK_HEATWAVE] = settlements_df['heatwave_hazard']
    if 'drought_hazard' in settlements_df.columns:
        settlements_df[SET_CLIMATE_RISK_DROUGHT] = settlements_df['drought_hazard']

    # Fill NaN with 0 (settlements outside coverage)
    for col in [SET_NORMALIZED_CLIMATE_HAZARD, SET_CLIMATE_HAZARD,
                SET_CLIMATE_VULNERABILITY, SET_CLIMATE_PRIORITY, SET_CLIMATE_RISK_HEATWAVE, SET_CLIMATE_RISK_DROUGHT]:
        if col in settlements_df.columns:
            settlements_df[col] = settlements_df[col].fillna(0)

    logger.info(f"Mapped climate risk to {len(settlements_df)} settlements")

    return settlements_df


# =============================================================================
# MAIN PROCESSING FUNCTION
# =============================================================================

def process_climate_data(
    climate_folder: str,
    admin3_shapefile: str,
    settlements_df: pd.DataFrame,
    specs_path: Optional[str] = None,
    allow_neutral_vulnerability: bool = False
) -> pd.DataFrame:
    """Main entry point: process climate data and add hazard outputs to settlements.

    This function orchestrates the full pipeline:
    1. Load climate configuration from specs file
    2. Classify and load climate data files by temporal resolution and data type
    3. Auto-discover hazard modules and run each hazard calculation
    4. Combine hazards into a compound hazard score
    5. Map hazards to settlements and compute ClimatePriority

    Args:
        climate_folder: Path to folder with climate CSV files.
        admin3_shapefile: Path to admin-3 level shapefile.
        settlements_df: OnSSET settlements DataFrame.
        specs_path: Optional path to specs Excel file for configuration.

    Returns:
        Settlements DataFrame with climate outputs added (naming kept backward compatible):
        - ClimateHazard (compound hazard)
        - ClimateRiskHeatwave / ClimateRiskDrought (legacy per-hazard columns)
        - ClimateHazard{HazardLabel} (per-hazard columns for all discovered hazards)
        - ClimateVulnerability, ClimatePriority
        - Admin3ID
    """
    logger.info("=" * 60)
    logger.info("Starting climate data processing...")
    logger.info("=" * 60)

    # Step 1: Discover hazard modules and load configuration (includes hazard schemas)
    hazard_modules = _filter_enabled_hazards(discover_hazard_modules())
    if not hazard_modules:
        raise ValueError('No hazard modules discovered. Expected modules in onsset.climate_calculations.')
    config = load_climate_config(specs_path, hazard_modules=hazard_modules)

    # Step 1b: Strict file requirements (filename-based)
    all_files = _list_input_files(climate_folder)
    for module in hazard_modules:
        hazard_name = str(getattr(module, 'HAZARD_NAME', '')).strip().lower() or module.__name__
        required = list(getattr(module, 'REQUIRED_FILE_GLOBS', []) or [])
        _require_any_file(
            folder_path=climate_folder,
            all_files=all_files,
            hazard_name=hazard_name,
            required_globs=required,
        )

    # Step 2: Load admin-3 boundaries
    logger.info(f"Loading admin-3 shapefile: {admin3_shapefile}")
    admin3_gdf = gpd.read_file(admin3_shapefile)
    if admin3_gdf.crs is None or admin3_gdf.crs.to_epsg() != 4326:
        admin3_gdf = admin3_gdf.to_crs(epsg=4326)
    logger.info(f"Loaded {len(admin3_gdf)} admin-3 regions")

    # Step 3: Initialize loader and classify files (strict: filename-based)
    loader = ClimateDataLoader(climate_folder, config)

    # Get available data combinations (temporal resolution x data type)
    available = loader.get_available_combinations()
    logger.info(f"Available data combinations: {[(t.value, d.value) for t, d in available]}")

    # Column mapping is strict: use configured column names.
    detected_columns: Dict[str, str] = {
        'latitude': config['lat_column'],
        'longitude': config['lon_column'],
        'date': config['date_column'],
    }
    if 'temp_column' in config and config['temp_column'] is not None:
        detected_columns['temperature'] = config['temp_column']
    if 'precip_column' in config and config['precip_column'] is not None:
        detected_columns['precipitation'] = config['precip_column']

    # Step 4: Run all enabled hazard modules (fail-fast on missing inputs)
    hazard_dfs: List[pd.DataFrame] = []
    hazard_label_by_name: Dict[str, str] = {}
    for module in hazard_modules:
        hazard_name = str(getattr(module, 'HAZARD_NAME', '')).strip().lower()
        raw_label = getattr(module, 'HAZARD_LABEL', None)
        hazard_label_by_name[hazard_name] = _camelize_label(raw_label) if raw_label else _camelize_label(hazard_name)
        logger.info(f"Running hazard module: {hazard_name}")
        df = module.calculate_hazard(loader, admin3_gdf, config, detected_columns)
        hazard_dfs.append(df)

    # Step 5: Calculate compound hazard across all returned hazards
    compound_hazard_df = calculate_compound_hazard(hazard_dfs, hazard_modules, config)

    # Step 6: Map to settlements
    settlements_df = map_risk_to_settlements(
        settlements_df,
        compound_hazard_df,
        admin3_gdf,
        config,
        hazard_label_by_name=hazard_label_by_name,
        allow_neutral_vulnerability=allow_neutral_vulnerability,
    )

    logger.info("=" * 60)
    logger.info("Climate data processing complete.")
    logger.info("=" * 60)

    return settlements_df


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================
def get_risk_column_names() -> Dict[str, str]:
    """Get dictionary of risk column names for use in onsset.py."""
    return {
        'normalized_hazard': SET_NORMALIZED_CLIMATE_HAZARD,
        # Backward-compatible key name; value is the compound hazard column.
        'compound': SET_CLIMATE_HAZARD,
        'heatwave': SET_CLIMATE_RISK_HEATWAVE,
        'drought': SET_CLIMATE_RISK_DROUGHT,
        'admin3_id': SET_ADMIN3_ID,
    }

