import numpy as np
import geopandas as gpd
import pandas as pd
import pytest
from types import SimpleNamespace
from shapely.geometry import Polygon

from onsset.climate_algorithm import (
    SET_ADMIN3_ID,
    SET_CLIMATE_HAZARD,
    SET_CLIMATE_PRIORITY,
    SET_CLIMATE_VULNERABILITY,
    SET_NORMALIZED_CLIMATE_HAZARD,
    calculate_compound_hazard,
    map_risk_to_settlements,
    process_climate_data,
)


def _build_inputs(include_wealth: bool = True):
    settlements_df = pd.DataFrame(
        {
            'X_deg': [0.2, 0.3, 0.7, 0.8, 2.2, 2.7],
            'Y_deg': [0.2, 0.7, 0.3, 0.8, 0.2, 0.7],
            'NormalizedRelativeWealth': [0.05, 0.25, 0.55, 0.85, 0.15, 0.95],
            'NormalizedTravelHours': [0.90, 0.70, 0.40, 0.10, 0.80, 0.20],
        }
    )
    if not include_wealth:
        settlements_df = settlements_df.drop(columns=['NormalizedRelativeWealth'])

    risk_df = pd.DataFrame(
        {
            'admin3_id': [1, 2],
            'heatwave_hazard': [0.2, 0.8],
            'drought_hazard': [0.1, 0.6],
            'compound_hazard': [0.4, 0.9],
        }
    )

    admin3_gdf = gpd.GeoDataFrame(
        {
            'admin3_id': [1, 2],
            'admin3_name': ['A', 'B'],
            'geometry': [
                Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]),
                Polygon([(2.0, 0.0), (3.0, 0.0), (3.0, 1.0), (2.0, 1.0)]),
            ],
        },
        crs='EPSG:4326',
    )

    config = {
        'admin3_id_column': 'admin3_id',
        'admin3_name_column': 'admin3_name',
    }

    return settlements_df, risk_df, admin3_gdf, config


def test_climate_priority_populated():
    settlements_df, risk_df, admin3_gdf, config = _build_inputs()

    result = map_risk_to_settlements(
        settlements_df.copy(),
        risk_df,
        admin3_gdf,
        config,
    )

    assert SET_CLIMATE_PRIORITY in result.columns
    assert result[SET_CLIMATE_PRIORITY].notna().all()
    assert result[SET_CLIMATE_PRIORITY].nunique() > 1
    assert np.isclose(
        result[SET_CLIMATE_PRIORITY],
        result[SET_CLIMATE_HAZARD] * result[SET_CLIMATE_VULNERABILITY],
    ).all()
    assert result.columns.tolist().count('compound_hazard') == 1
    assert SET_NORMALIZED_CLIMATE_HAZARD in result.columns
    assert SET_ADMIN3_ID in result.columns


def test_missing_vulnerability_raises():
    settlements_df, risk_df, admin3_gdf, config = _build_inputs(include_wealth=False)

    with pytest.raises(ValueError):
        map_risk_to_settlements(
            settlements_df.copy(),
            risk_df,
            admin3_gdf,
            config,
        )


def test_neutral_vulnerability_opt_in():
    settlements_df, risk_df, admin3_gdf, config = _build_inputs(include_wealth=False)

    result = map_risk_to_settlements(
        settlements_df.copy(),
        risk_df,
        admin3_gdf,
        config,
        allow_neutral_vulnerability=True,
    )

    assert np.isclose(result[SET_CLIMATE_VULNERABILITY], 0.5).all()
    assert np.isclose(
        result[SET_CLIMATE_PRIORITY],
        result[SET_CLIMATE_HAZARD] * 0.5,
    ).all()


def test_map_risk_idempotent():
    settlements_df, risk_df, admin3_gdf, config = _build_inputs()

    first = map_risk_to_settlements(
        settlements_df.copy(),
        risk_df,
        admin3_gdf,
        config,
    )

    second = map_risk_to_settlements(
        first.copy(),
        risk_df,
        admin3_gdf,
        config,
    )

    assert SET_CLIMATE_PRIORITY in second.columns
    assert np.isclose(second[SET_CLIMATE_PRIORITY], first[SET_CLIMATE_PRIORITY]).all()


def test_compound_hazard_pipeline():
    settlements_df, _, admin3_gdf, config = _build_inputs()

    hazard_modules = [
        SimpleNamespace(
            HAZARD_NAME='heatwave',
            HAZARD_LABEL='Heatwave',
            OUTPUT_COLUMN='heatwave_hazard',
        ),
        SimpleNamespace(
            HAZARD_NAME='drought',
            HAZARD_LABEL='Drought',
            OUTPUT_COLUMN='drought_hazard',
        ),
    ]

    hazard_dfs = [
        pd.DataFrame(
            {
                'admin3_id': [1, 2],
                'admin3_name': ['A', 'B'],
                'heatwave_hazard': [0.2, 0.8],
            }
        ),
        pd.DataFrame(
            {
                'admin3_id': [1, 2],
                'admin3_name': ['A', 'B'],
                'drought_hazard': [0.1, 0.6],
            }
        ),
    ]

    risk_df = calculate_compound_hazard(hazard_dfs, hazard_modules, config)

    result = map_risk_to_settlements(
        settlements_df.copy(),
        risk_df,
        admin3_gdf,
        config,
    )

    assert SET_CLIMATE_PRIORITY in result.columns
    assert result[SET_CLIMATE_PRIORITY].notna().all()
    assert result[SET_CLIMATE_PRIORITY].nunique() > 1
    assert np.isclose(
        result[SET_CLIMATE_PRIORITY],
        result[SET_CLIMATE_HAZARD] * result[SET_CLIMATE_VULNERABILITY],
    ).all()
    assert result.columns.tolist().count('compound_hazard') == 1
    assert SET_NORMALIZED_CLIMATE_HAZARD in result.columns
    assert SET_ADMIN3_ID in result.columns


def test_process_climate_data_with_cached_hazards(tmp_path):
    settlements_df = pd.DataFrame(
        {
            'X_deg': [0.2, 0.3, 2.2, 2.7],
            'Y_deg': [0.2, 0.7, 0.2, 0.7],
            'NormalizedRelativeWealth': [0.05, 0.25, 0.15, 0.95],
            'NormalizedTravelHours': [0.90, 0.70, 0.80, 0.20],
        }
    )

    admin3_gdf = gpd.GeoDataFrame(
        {
            'GID_3': [1, 2],
            'NAME_3': ['A', 'B'],
            'geometry': [
                Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]),
                Polygon([(2.0, 0.0), (3.0, 0.0), (3.0, 1.0), (2.0, 1.0)]),
            ],
        },
        crs='EPSG:4326',
    )
    admin3_shapefile = tmp_path / 'admin3.shp'
    admin3_gdf.to_file(admin3_shapefile)

    pd.DataFrame(
        {
            'GID_3': [1, 2],
            'NAME_3': ['A', 'B'],
            'heatwave_hazard': [0.2, 0.8],
        }
    ).to_csv(tmp_path / 'heatwave_hazard.csv', index=False)
    pd.DataFrame(
        {
            'GID_3': [1, 2],
            'NAME_3': ['A', 'B'],
            'drought_hazard': [0.1, 0.6],
        }
    ).to_csv(tmp_path / 'drought_hazard.csv', index=False)

    result = process_climate_data(
        climate_folder=None,
        admin3_shapefile=str(admin3_shapefile),
        settlements_df=settlements_df,
        precomputed_hazards_folder=str(tmp_path),
    )

    assert SET_CLIMATE_PRIORITY in result.columns
    assert result[SET_CLIMATE_PRIORITY].notna().all()
    assert result[SET_CLIMATE_PRIORITY].nunique() > 1
    assert np.isclose(
        result[SET_CLIMATE_PRIORITY],
        result[SET_CLIMATE_HAZARD] * result[SET_CLIMATE_VULNERABILITY],
    ).all()
