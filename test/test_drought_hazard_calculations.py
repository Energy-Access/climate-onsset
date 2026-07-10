"""Behavioral tests for drought_calculation.calculate_hazard."""

import numpy as np
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon
from onsset.climate_calculations import drought_calculation
from scipy.stats import gamma, norm


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


def test_spi_math_matches_scipy_reference():
    """Pin the SPI transform in ``_compute_spi_for_cell`` to a scipy.stats reference.

    SPI-k is defined canonically as ``norm.ppf(gamma.cdf(P_k, *gamma_fit))`` applied
    to a k-month rolling precipitation accumulation. This test feeds a synthetic
    precipitation series drawn from a known gamma distribution through the module's
    SPI computation and checks the result against an independent composition of
    ``scipy.stats.gamma.fit`` + ``gamma.cdf`` + ``norm.ppf`` on the same series.

    Implementation note: the module fits the gamma distribution *per calendar month*
    (standard SPI practice) and uses a mixed distribution with mass at zero
    (``H = (1 - q) + q * G``). The reference below mirrors that exact approach rather
    than a single-window MLE fit, so the comparison validates that the implementation
    matches the canonical definition without hand-pinning numerical values. Because
    both paths call the identical scipy primitives, the match is effectively exact;
    rtol=1e-2 leaves headroom for any gamma-fit numeric drift.
    """
    from onsset.climate_calculations import drought_calculation
    from scipy.stats import gamma, norm

    np.random.seed(42)
    n_months = 360  # ~30 years of monthly data
    dates = pd.date_range('1971-01-01', periods=n_months, freq='MS')
    precip = gamma.rvs(a=2.0, scale=30.0, size=n_months)

    df_cell = pd.DataFrame({'date': dates, 'tp_mm_month': precip})

    scale = 3  # 3-month SPI (typical default)
    baseline_start = int(dates.min().year)
    baseline_end = int(dates.max().year)  # baseline window == full series

    spi_df = drought_calculation._compute_spi_for_cell(
        df_cell,
        'date',
        'tp_mm_month',
        scale,
        baseline_start,
        baseline_end,
        baseline_params=None,  # force the cell-specific gamma fit path
    )

    # Independent reference: replicate the canonical, month-stratified SPI transform.
    ref = df_cell.sort_values('date').set_index('date')
    ref['P_k'] = ref['tp_mm_month'].rolling(window=scale, min_periods=scale).sum()
    ref['month'] = ref.index.month

    expected = pd.Series(index=ref.index, dtype=float)
    for m in range(1, 13):
        series = ref.loc[ref['month'] == m, 'P_k']
        baseline_values = series.dropna()
        positive = baseline_values[baseline_values > 0]

        shape, _loc, scale_param = gamma.fit(positive, floc=0)
        q = len(positive) / len(baseline_values)

        x = series.values
        x_clipped = np.maximum(x, 0.0001)
        G = gamma.cdf(x_clipped, shape, loc=0, scale=scale_param)
        H = (1.0 - q) + q * G
        H[x <= 0] = (1.0 - q)
        H = np.clip(H, 1e-6, 1 - 1e-6)
        expected.loc[series.index] = norm.ppf(H)

    result_spi = spi_df.set_index('date')['spi']
    expected_aligned = expected.loc[result_spi.index]

    assert not result_spi.empty
    assert np.allclose(result_spi.to_numpy(), expected_aligned.to_numpy(), rtol=1e-2)