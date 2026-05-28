"""Performs a regression test of the OnSSET modules

Notes
-----

Run ``python test/test_runner.py`` to update the test files,
if an intended modification is made to the codebase which
changes the contents of the output files.

"""

import filecmp
import os
from shutil import copyfile
from tempfile import TemporaryDirectory

import pytest

from onsset.runner import calibration, scenario


def _regression_fixture_paths():
    pv_path = os.path.join('test', 'test_data', 'pv_test.csv')
    wind_path = os.path.join('test', 'test_data', 'wind_test.csv')
    mv_path = os.path.join('test', 'test_data', 'mv_lines_test.csv')

    if not all(os.path.exists(path) for path in [pv_path, wind_path, mv_path]):
        return None

    return pv_path, wind_path, mv_path


def run_analysis(tmpdir, pv_path, wind_path, mv_path):
    """

    Arguments
    ---------
    tmpdir : str
        Temporary directory to use for the calculated files

    Returns
    -------
    tuple
        Returns a tuple of bool for whether the summary or full files match

    """

    specs_path = os.path.join('test', 'test_data', 'dj-specs-test.xlsx')
    csv_path = os.path.join('test', 'test_data', 'dj-test.csv')
    calibrated_csv_path = os.path.join(tmpdir, 'dj-calibrated.csv')
    specs_path_calib = os.path.join(tmpdir, 'dj-specs-test-calib.xlsx')

    calibration(specs_path, csv_path, specs_path_calib, calibrated_csv_path)

    scenario(
        specs_path_calib,
        calibrated_csv_path,
        tmpdir,
        tmpdir,
        pv_path=pv_path,
        wind_path=wind_path,
        mv_path=mv_path,
    )

    actual = os.path.join(tmpdir, 'dj-1-1_1_1_1_0_0_summary.csv')
    expected = os.path.join('test', 'test_results', 'expected_summary.csv')
    summary = filecmp.cmp(actual, expected)
    if summary == False:
        print(actual)

    actual = os.path.join(tmpdir, 'dj-1-1_1_1_1_0_0.csv')
    expected = os.path.join('test', 'test_results', 'expected_full.csv')
    full = filecmp.cmp(actual, expected)
    return summary, full


def test_regression_summary():
    """A regression test to track changes to the summary results of OnSSET

    """

    with TemporaryDirectory() as tmpdir:
        fixture_paths = _regression_fixture_paths()
        if fixture_paths is None:
            pytest.skip("Regression fixtures missing — see A5 example dataset")
        summary, full = run_analysis(tmpdir, *fixture_paths)

    assert summary
    assert full


def update_test_file():
    """A utility function to produce a new test file if intended changes are made
    """
    tmpdir = '.'

    fixture_paths = _regression_fixture_paths()
    if fixture_paths is None:
        print("Regression fixtures missing — see A5 example dataset")
        return

    summary, actual = run_analysis(tmpdir, *fixture_paths)

    actual = os.path.join(tmpdir, 'dj-1-1_1_1_1_0_0_summary.csv')
    expected = os.path.join('test', 'test_results', 'expected_summary.csv')
    if not summary:
        copyfile(actual, expected)

    actual = os.path.join(tmpdir, 'dj-1-1_1_1_1_0_0.csv')
    expected = os.path.join('test', 'test_results', 'expected_full.csv')
    if not actual:
        copyfile(actual, expected)


if __name__ == '__main__':

    update_test_file()
