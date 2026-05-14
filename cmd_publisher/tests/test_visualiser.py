"""Unit tests for the visualiser's Savitzky-Golay derivative coefficients.
"""
import matplotlib
matplotlib.use("Agg") 

import numpy as np

from cmd_publisher.visualiser import SG_COEFFS, SG_WINDOW, DT_SECONDS


def test_savgol_constant_signal_yields_zero_derivatives():
    y = np.full(SG_WINDOW, 5.0)
    assert abs(SG_COEFFS[0] @ y - 5.0) < 1e-9
    assert abs(SG_COEFFS[1] @ y / DT_SECONDS) < 1e-9
    assert abs(SG_COEFFS[2] @ y / DT_SECONDS**2) < 1e-9
    assert abs(SG_COEFFS[3] @ y / DT_SECONDS**3) < 1e-9


def test_savgol_linear_signal_yields_constant_velocity():
    t = np.arange(SG_WINDOW) * DT_SECONDS
    y = 3.0 * t
    assert abs(SG_COEFFS[1] @ y / DT_SECONDS - 3.0) < 1e-9
    assert abs(SG_COEFFS[2] @ y / DT_SECONDS**2) < 1e-9


def test_savgol_quadratic_signal_yields_constant_acceleration():
    t = np.arange(SG_WINDOW) * DT_SECONDS
    y = 2.0 * t * t
    assert abs(SG_COEFFS[2] @ y / DT_SECONDS**2 - 4.0) < 1e-9
    assert abs(SG_COEFFS[3] @ y / DT_SECONDS**3) < 1e-9


def test_savgol_cubic_signal_yields_constant_jerk():
    t = np.arange(SG_WINDOW) * DT_SECONDS
    y = t * t * t
    assert abs(SG_COEFFS[3] @ y / DT_SECONDS**3 - 6.0) < 1e-9
