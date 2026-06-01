"""Behavioral tests for drought_calculation.calculate_hazard."""

import numpy as np
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon


def _build_drought_inputs(avg_precip_by_admin, *, start='2000-01-01', months=60):
    admin_ids = list(avg_precip_by_admin.keys())
    dates = pd.date_range(start, periods=months, freq='MS')

    admin3_rows = []
    precip_rows = []
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
        precip_spec = avg_precip_by_admin[admin_id]
        if isinstance(precip_spec, (list, tuple, np.ndarray, pd.Series)):
            if len(precip_spec) != len(dates):
                raise ValueError('Per-admin precipitation series must match the requested number of months')
            precip_values = [float(v) for v in precip_spec]
        else:
            precip_values = [float(precip_spec)] * len(dates)

        for d, precip in zip(dates, precip_values):
            precip_rows.append(
                {
                    'lat': lat,
                    'lon': lon,
                    'date': d,
                    'tp_mm_month': precip,
                }
            )

    monthly_precip_df = pd.DataFrame(precip_rows)
    admin3_gdf = gpd.GeoDataFrame(admin3_rows, crs='EPSG:4326')

    config = {
        'admin3_id_column': 'admin3_id',
        'admin3_name_column': 'admin3_name',
        'spi_scale': 3,
        'spi_baseline_start': int(dates.min().year),
        'spi_baseline_end': int(dates.max().year),
    }
    detected_columns = {
        'latitude': 'lat',
        'longitude': 'lon',
        'date': 'date',
        'precipitation': 'tp_mm_month',
    }

    return monthly_precip_df, admin3_gdf, config, detected_columns


def test_drought_constant_precip_no_signal():
    from onsset.climate_calculations import drought_calculation

    # Identical precipitation time series for every admin (no spatial signal),
    # but time-varying so the SPI baseline can be fit.
    precip_series = (50.0 + (np.arange(60) % 12) + 0.1 * (np.arange(60) // 12)).astype(float)

    monthly_precip_df, admin3_gdf, config, detected_columns = _build_drought_inputs(
        {
            1: precip_series,
            2: precip_series,
            3: precip_series,
        },
        months=60,
    )

    result = drought_calculation.calculate_hazard(
        monthly_precip_df,
        admin3_gdf,
        config,
        detected_columns,
    )

    values = result.sort_values('admin3_id')['drought_hazard'].to_numpy(dtype=float)
    assert np.isclose(values, values[0], atol=1e-6, rtol=0).all()


def test_drought_drier_admin_ranks_higher():
    from onsset.climate_calculations import drought_calculation

    monthly_precip_df, admin3_gdf, config, detected_columns = _build_drought_inputs(
        {
            1: 20.0,  # drier
            2: 80.0,  # wetter
        },
        months=60,
    )

    result = drought_calculation.calculate_hazard(
        monthly_precip_df,
        admin3_gdf,
        config,
        detected_columns,
    )

    hazard_by_id = result.set_index('admin3_id')['drought_hazard']
    assert hazard_by_id.loc[1] > hazard_by_id.loc[2]


def test_drought_percentile_rank_distribution():
    from onsset.climate_calculations import drought_calculation

    monthly_precip_df, admin3_gdf, config, detected_columns = _build_drought_inputs(
        {
            1: 20.0,
            2: 35.0,
            3: 50.0,
            4: 65.0,
            5: 80.0,
        },
        months=60,
    )

    result = drought_calculation.calculate_hazard(
        monthly_precip_df,
        admin3_gdf,
        config,
        detected_columns,
    )

    values_sorted = np.sort(result['drought_hazard'].to_numpy(dtype=float))
    expected = np.linspace(1.0 / 5.0, 1.0, 5)
    assert np.isclose(values_sorted, expected, rtol=1e-6, atol=0).all()
