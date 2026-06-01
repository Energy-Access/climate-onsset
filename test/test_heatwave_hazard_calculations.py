"""Behavioral tests for heatwave_calculation.calculate_hazard."""

import numpy as np
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from onsset.climate_calculations import heatwave_calculation


def make_temp_factory(temp_df):
    def factory():
        yield ("fake_file.csv", temp_df)

    return factory


def _build_heatwave_inputs(temps_by_admin, *, start='2001-01-01', days=365):
    admin_ids = list(temps_by_admin.keys())
    dates = pd.date_range(start, periods=days, freq='D')

    admin3_rows = []
    temp_rows = []

    for idx, admin_id in enumerate(admin_ids):
        x0 = float(idx) * 2.0
        poly = Polygon([(x0, 0.0), (x0 + 1.0, 0.0), (x0 + 1.0, 1.0), (x0, 1.0)])
        admin3_rows.append(
            {
                'admin3_id': admin_id,
                'admin3_name': f'Admin{admin_id}',
                'geometry': poly,
            }
        )

        lon = x0 + 0.5
        lat = 0.5

        spec = temps_by_admin[admin_id]
        if isinstance(spec, (list, tuple, np.ndarray, pd.Series)):
            if len(spec) != len(dates):
                raise ValueError('Per-admin temperature series must match the requested number of days')
            values = [float(v) for v in spec]
        else:
            values = [float(spec)] * len(dates)

        for d, t_c in zip(dates, values):
            temp_rows.append(
                {
                    'lat': lat,
                    'lon': lon,
                    'date': d,
                    't2m_max_C': t_c,
                }
            )

    temp_df = pd.DataFrame(temp_rows)
    admin3_gdf = gpd.GeoDataFrame(admin3_rows, crs='EPSG:4326')

    config = {
        'admin3_id_column': 'admin3_id',
        'admin3_name_column': 'admin3_name',
        'heatwave_threshold_c': 32.0,
        'heatwave_duration_days': 3,
    }
    detected_columns = {
        'latitude': 'lat',
        'longitude': 'lon',
        'date': 'date',
        'temperature': 't2m_max_C',
    }

    return temp_df, admin3_gdf, config, detected_columns


def test_heatwave_no_hot_days_returns_uniform():
    # Uniformly cool temperatures across all admins (no hot days anywhere).
    temp_df, admin3_gdf, config, detected_columns = _build_heatwave_inputs(
        {
            1: 280.0 - 273.15,
            2: 280.0 - 273.15,
            3: 280.0 - 273.15,
        },
        days=365,
    )

    daily_temp_iter_factory = make_temp_factory(temp_df)

    result = heatwave_calculation.calculate_hazard(
        daily_temp_iter_factory,
        admin3_gdf,
        config,
        detected_columns,
    )

    values = result.sort_values('admin3_id')[heatwave_calculation.OUTPUT_COLUMN].to_numpy(dtype=float)
    assert np.isclose(values, values[0], atol=1e-6, rtol=0).all()


def test_heatwave_hotter_admin_ranks_higher():
    # Admin 1 is always hot; admin 2 is always cool.
    temp_df, admin3_gdf, config, detected_columns = _build_heatwave_inputs(
        {
            1: 320.0 - 273.15,
            2: 280.0 - 273.15,
        },
        days=365,
    )

    daily_temp_iter_factory = make_temp_factory(temp_df)

    result = heatwave_calculation.calculate_hazard(
        daily_temp_iter_factory,
        admin3_gdf,
        config,
        detected_columns,
    )

    hazard_by_id = result.set_index('admin3_id')[heatwave_calculation.OUTPUT_COLUMN]
    assert hazard_by_id.loc[1] > hazard_by_id.loc[2]


def test_heatwave_percentile_rank_distribution():
    # Five admins with monotonically increasing mean temperatures.
    # Construct each admin's series with a different fraction of hot days so that
    # mean heatwave-days-per-year is strictly increasing.
    days = 365
    cold = 280.0 - 273.15
    hot = 320.0 - 273.15

    def mix(hot_days):
        return np.array([hot] * hot_days + [cold] * (days - hot_days), dtype=float)

    temp_df, admin3_gdf, config, detected_columns = _build_heatwave_inputs(
        {
            1: mix(0),
            2: mix(days // 4),
            3: mix(days // 2),
            4: mix((3 * days) // 4),
            5: mix(days),
        },
        days=days,
    )

    daily_temp_iter_factory = make_temp_factory(temp_df)

    result = heatwave_calculation.calculate_hazard(
        daily_temp_iter_factory,
        admin3_gdf,
        config,
        detected_columns,
    )

    values_sorted = np.sort(result[heatwave_calculation.OUTPUT_COLUMN].to_numpy(dtype=float))
    expected = np.linspace(1.0 / 5.0, 1.0, 5)
    assert np.isclose(values_sorted, expected, rtol=1e-6, atol=0).all()
