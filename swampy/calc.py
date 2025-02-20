"""
SWAMP
"""
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

warnings.filterwarnings("ignore", message=r"note 'stable' PRISM file for [0-9]{8} not found")
warnings.filterwarnings("ignore", message=r"date [0-9]{7} not in detected ALEXI ET available dates")

_HERE = Path(__file__).parent
_ORIG = _HERE / "../orig"

_NLAT, _NLON = (720, 1150)


def _get_grid():
    # Define output grid
    # NOTE: this is 3.8-km in lat and ~ 4.4-km in lon
    lat = np.linspace(23, 50, _NLAT)
    lon = np.linspace(-125, -65, _NLON)
    grid = xr.Dataset(
        coords={
            "lat": (("lat",), lat, {"long_name": "Latitude", "units": "degree_north"}),
            "lon": (("lon",), lon, {"long_name": "Longitude", "units": "degree_east"}),
        }
    )
    return grid


GRID = _get_grid()


def _get_coeffs_ds(*, mask_neg=True):
    # For now, use the coeffs that already exist
    # NOTE: slp is the one that gets used in the current original SWAMP
    # NOTE: this array has 39% 0 values and 17% -21.52 values, the rest positive but < 1
    # NOTE: some of the -21.52 values are over land (TX ~ 27 and below, FL ~ 26 and below), makes results weird
    slp_coeffs = np.loadtxt(_ORIG / "PROCESS_DAILY/slp_weights.txt")
    int_coeffs = np.loadtxt(_ORIG / "PROCESS_DAILY/int_weights.txt")
    assert slp_coeffs.shape == int_coeffs.shape == (_NLAT, _NLON)
    c0 = int_coeffs
    c1 = slp_coeffs

    ds = GRID.copy()
    ds["c0"] = (("lat", "lon"), c0, {"long_name": "Intercept coefficient"})
    ds["c1"] = (("lat", "lon"), c1, {"long_name": "Slope coefficient"})
    if mask_neg:
        ds["c1"] = ds.c1.where(ds.c1 > 0, np.nan)

    return ds


C = _get_coeffs_ds()

_DEFAULT_METPY_INTERP_KWS = dict(interp_type="rbf", hres=0.1)


def _ic_crn(date, **kwargs):
    from metpy.interpolate import interpolate_to_grid, remove_nan_observations

    from .load import get_crn

    interp_kws = {**_DEFAULT_METPY_INTERP_KWS, **kwargs}

    # IC based on CRN
    # TODO: use interpolation to 25 cm using multiple levels
    df = get_crn([date])
    x = df["LONGITUDE"]
    y = df["LATITUDE"]
    v = df["SOIL_MOISTURE_20_DAILY"].copy()
    v.loc[v == -99] = np.nan
    x, y, v = remove_nan_observations(x, y, v)
    xg, yg, vg = interpolate_to_grid(x, y, v, **interp_kws)
    ic = xr.DataArray(
        data=vg, coords={"lat": yg[:, 0], "lon": xg[0, :]}, dims=("lat", "lon")
    ).interp(lat=GRID.lat, lon=GRID.lon)
    return ic


def _ic_awc(date, **kwargs):
    # AWC: available water content
    # This comes from the original code and was suggested by Temple Lee
    from metpy.interpolate import interpolate_to_grid, remove_nan_observations

    from .load import get_crn

    interp_kws = {**_DEFAULT_METPY_INTERP_KWS, **kwargs}

    df = get_crn([date])

    x = df["LONGITUDE"].copy()
    y = df["LATITUDE"].copy()
    v5cm = df["SOIL_MOISTURE_5_DAILY"].copy()
    v5cm.loc[v5cm == -99] = np.nan
    # x, y, v5cm = remove_nan_observations(x, y, v5cm)
    # xg, yg, vg5cm = interpolate_to_grid(x, y, v5cm, **interp_kws)

    # x = df["LONGITUDE"].copy()
    # y = df["LATITUDE"].copy()
    v10cm = df["SOIL_MOISTURE_10_DAILY"].copy()
    v10cm.loc[v10cm == -99] = np.nan
    # x, y, v10cm = remove_nan_observations(x, y, v10cm)
    # xg, yg, vg10cm = interpolate_to_grid(x, y, v10cm, **interp_kws)

    # x = df["LONGITUDE"].copy()
    # y = df["LATITUDE"].copy()
    v20cm = df["SOIL_MOISTURE_20_DAILY"].copy()
    v20cm.loc[v20cm == -99] = np.nan
    # x, y, v20cm = remove_nan_observations(x, y, v20cm)
    # xg, yg, vg20cm = interpolate_to_grid(x, y, v20cm, **interp_kws)

    # awc = 7.5 * vg5cm + 7.5 * vg10cm + 20 * vg20cm

    # TODO: might be better to add gridded instead of adding first and then gridding
    awcp = 7.5 * v5cm + 7.5 * v10cm + 20 * v20cm
    x, y, awc = remove_nan_observations(x, y, awcp)
    xg, yg, awc = interpolate_to_grid(x, y, awc, **interp_kws)

    ic = xr.DataArray(
        data=awc,
        coords={"lat": yg[:, 0], "lon": xg[0, :]},
        dims=("lat", "lon"),
    ).interp(
        lat=GRID.lat,
        lon=GRID.lon,
    )

    return ic


def _ic_zero(x):
    # 0 but using the land mask from P - ET or c
    # TODO: maybe better to use regionmask since inland NaN areas seem to change
    return x.where(x.isnull(), 0)


def run(start, end, *, ic=None, ic_kws=None, use_intercept=False, quiet=False):
    """Compute gridded soil moisture using the SWAMP algorithm.

    Parameters
    ----------
    start, end
        Passed to `pandas.date_range` to generate the days to run.
        Date `start` gets the IC.
    ic : {'crn', 'awc', 'zero'} or 0 or xarray.DataArray, optional
        Initial condition. Defaults to zero.
    ic_kws : dict, optional
        For example, MetPy interpolate-to-grid settings.
    use_intercept : bool
        Apply the intercept term (in addition to the slope term) to P - ET.
    quiet : bool
        Don't print info messages.
    """
    from .load import get_alexi, get_prism

    days = pd.date_range(start, end, freq="D")
    ntime = len(days)

    # Compute P - ET at the grid
    if not quiet:
        print("loading PRISM P")
    p = get_prism(days).ppt
    if not quiet:
        print("loading ALEXI ET")
    et = get_alexi(days).et
    if not quiet:
        print("computing P - ET")
    # TODO: better interp, e.g. xESMF conservative (can store the weight file)
    p_minus_et = p.interp(lat=GRID.lat, lon=GRID.lon) - et.interp(lat=GRID.lat, lon=GRID.lon)
    p_minus_et.attrs.update(long_name="P - ET", units="mm")

    # Initialize sm dataset
    if not quiet:
        print("computing SM")
    soil_depth_cm = 25  # soil depth of interest
    soil_depth_mm = soil_depth_cm * 10
    ds = C.copy()
    ds["sm"] = (
        ("time", "lat", "lon"),
        np.empty((ntime, _NLAT, _NLON)),
        {
            "long_name": "Soil moisture",
            "units": "m3 m-3",
            "description": f"Fractional volumetric water content for the top {soil_depth_cm} cm of soil",
            "long_units": "(m3 water) m-3",
        },
    )
    ds["smn"] = (
        ("time", "lat", "lon"),
        np.empty((ntime, _NLAT, _NLON)),
        {
            "long_name": "Soil moisture",
            "units": "m3 m-3",
            "description": f"Fractional volumetric water content for the top {soil_depth_cm} cm of soil without using the coefficients",
            "long_units": "(m3 water) m-3",
        },
    )
    ds["time"] = days
    ds["d"] = (
        (),
        soil_depth_mm,
        {
            "long_name": "Soil depth",
            "units": "mm",
            "description": "Multiply fractional volumetric soil moisture by this to convert from m3 m-3 to mm",
        },
    )

    if ic_kws is None:
        ic_kws = {}

    if ic is None or ic == 0 or isinstance(ic, str) and ic.lower() == "zero":
        ic = _ic_zero(ds.c1)
    elif isinstance(ic, str) and ic.lower() == "crn":
        ic = _ic_crn(start, **ic_kws)
    elif isinstance(ic, str) and ic.lower() == "awc":
        ic = _ic_awc(start, **ic_kws)
    elif isinstance(ic, xr.DataArray):
        pass
    else:
        raise ValueError(f"invalid `ic` setting {ic!r}")

    ds["sm"].loc[dict(time=days[0])] = ic
    ds["smn"].loc[dict(time=days[0])] = ic

    for i in range(1, ntime):
        # sm(i) = sm(i - 1) + [P - ET during day i]    Eq. 2 in SWAMP paper draft
        delta_mm_no_coeff = p_minus_et.isel(time=i)
        # ^ for "smn" in the Fortran (no weighting coeffs used)
        delta_mm = ds.c1 * delta_mm_no_coeff
        if use_intercept:
            delta_mm += ds.c0

        # Multiplying by the soil depth, we convert the previous sm field from m3 m-3 to mm
        # This gives it units like: mm water per <soil depth> depth of soil per unit area
        # Then, we convert back
        ds["sm"].loc[dict(time=days[i])] = np.clip(
            (soil_depth_mm * ds.sm.isel(time=i - 1) + delta_mm) / soil_depth_mm,
            0,
            1,
        )
        ds["smn"].loc[dict(time=days[i])] = np.clip(
            (soil_depth_mm * ds.smn.isel(time=i - 1) + delta_mm_no_coeff) / soil_depth_mm,
            0,
            1,
        )

    ds = ds.drop_vars(["c0", "c1"])

    return ds
