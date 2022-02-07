"""Test sfincs utils"""

from datetime import datetime
from pyproj.crs.crs import CRS
from affine import Affine
import pytest
from os.path import join, dirname, abspath, isfile
import numpy as np
import xarray as xr
from shapely.geometry import MultiLineString, Point
import geopandas as gpd
import copy

from hydromt_sfincs import utils

EXAMPLEDIR = join(dirname(abspath(__file__)), "..", "examples", "sfincs_riverine")


def test_inp(tmpdir):
    conf = utils.read_inp(join(EXAMPLEDIR, "sfincs.inp"))
    assert isinstance(conf, dict)
    assert "mmax" in conf
    fn_out = str(tmpdir.join("sfincs.inp"))
    utils.write_inp(fn_out, conf)
    conf1 = utils.read_inp(fn_out)
    assert conf == conf1

    shape, transform, crs = utils.get_spatial_attrs(conf)
    assert isinstance(crs, CRS)
    assert isinstance(transform, Affine)
    assert len(shape) == 2
    crs = utils.get_spatial_attrs(conf, crs=4326)[-1]
    assert crs.to_epsg() == 4326
    conf.pop("epsg")
    crs = utils.get_spatial_attrs(conf)[-1]
    assert crs is None

    with pytest.raises(NotImplementedError, match="Rotated grids"):
        conf.update(rotation=1)
        utils.get_spatial_attrs(conf)
    with pytest.raises(ValueError, match='"mmax" or "nmax"'):
        conf.pop("mmax")
        utils.get_spatial_attrs(conf)

    dt = utils.parse_datetime(conf["tref"])
    assert isinstance(dt, datetime)
    with pytest.raises(ValueError, match="Unknown type for datetime"):
        utils.parse_datetime(22)


def test_bin_map(tmpdir):
    conf = utils.read_inp(join(EXAMPLEDIR, "sfincs.inp"))
    shape = utils.get_spatial_attrs(conf)[0]
    ind = utils.read_binary_map_index(join(EXAMPLEDIR, "sfincs.ind"))
    msk = utils.read_binary_map(
        join(EXAMPLEDIR, "sfincs.msk"), ind, shape=shape, dtype="u1", mv=0
    )
    assert [v in [0, 1, 2, 3] for v in np.unique(msk)]
    assert ind.max() == ind[-1]

    fn_out = str(tmpdir.join("sfincs.ind"))
    utils.write_binary_map_index(fn_out, msk)
    ind1 = utils.read_binary_map_index(fn_out)
    assert np.all(ind == ind1)

    fn_out = str(tmpdir.join("sfincs.msk"))
    utils.write_binary_map(fn_out, msk, msk, dtype="u1")
    msk1 = utils.read_binary_map(fn_out, ind1, shape=shape, dtype="u1", mv=0)
    assert np.all(msk1 == msk1)


def test_structures(tmpdir, weirs):
    gdf = utils.structures2gdf(weirs)
    assert gdf.index.size == len(weirs)
    assert np.all(gdf.geometry.type == "LineString")
    weirs1 = utils.gdf2structures(gdf)
    for i in range(len(weirs)):
        assert sorted(weirs1[i].items()) == sorted(weirs[i].items())
    # single item MulitLineString should also work (often result of gpd.read_file)
    geoms = [MultiLineString([gdf.geometry.values[0].coords[:]])]
    struct = utils.gdf2structures(gpd.GeoDataFrame(geometry=geoms))
    assert struct[0]["x"] == weirs[0]["x"]
    # non LineString geomtry types raise a ValueError
    with pytest.raises(ValueError, match="Invalid geometry type"):
        utils.gdf2structures(gpd.GeoDataFrame(geometry=[Point(0, 0)]))
    # weir structure requires z data
    w = copy.deepcopy(weirs[0])
    w.pop("z")
    with pytest.raises(ValueError, match='"z" value missing'):
        utils.write_structures("fail", [w], stype="weir")
    # test I/O
    fn_out = str(tmpdir.join("test.weir"))
    utils.write_structures(fn_out, weirs, stype="WEIR")
    weirs2 = utils.read_structures(fn_out)
    weirs[1]["name"] = "WEIR02"  # a name is added when writing the file
    for i in range(len(weirs)):
        assert sorted(weirs2[i].items()) == sorted(weirs[i].items())


def test_subgrid_volume(tmpdir, elevation_data):
    nbins = 7
    # make some elevation data
    xi = elevation_data["xi"]
    yi = elevation_data["yi"]
    ele = elevation_data["elevation"]
    dx = np.diff(xi[0]).max()
    dy = np.diff(yi[:, 0]).max()
    ele_sort, volume = utils.subgrid_volume_level(ele, dx, dy)
    ele_discrete, volume_discrete = utils.subgrid_volume_discrete(ele_sort, volume, dx, dy, nbins=nbins)
    assert(len(volume)==len(ele.flatten()))
    depths = ele - ele.min()
    # compute maximum volume by simple addition, and check against highest value in volume
    max_vol = ((depths.max() - depths) * 100).sum()
    # check if the total volume on top of grid cell is equal to alternatively computed max.
    assert(max_vol == volume.max())
    # length of outputs must both be equal to bin size
    assert(len(ele_discrete) == nbins + 1 and len(volume_discrete) == nbins + 1)
    # lowest volume is zero, second lowest must be 1/nbins * max volume
    assert(np.isclose(volume_discrete[0], 0.))
    assert(np.isclose(volume_discrete[1], volume.max() / nbins))

def test_subgrid_depth(tmpdir, elevation_data, manning_data):
    nbins = 9
    xi = elevation_data["xi"]
    yi = elevation_data["yi"]
    ele = elevation_data["elevation"]
    manning = manning_data["manning"]
    dx = np.diff(xi[0]).max()
    dy = np.diff(yi[:, 0]).max()
    ele_sort, R = utils.subgrid_R_table(ele, manning, dx, dy)
    ele_discrete, R_discrete = utils.subgrid_R_discrete(ele_sort, R, nbins=nbins)
    assert(np.isclose(R_discrete[0], 0.)), f"First hydraulic radius is not zero, but {R_discrete[0]}"
    assert(np.isclose(ele_discrete[1], ele_sort.min()+ele_sort.ptp()/nbins)), f"Expected second elevation is {ele_sort.min()+ele_sort.ptp()/nbins}, instead found {ele_discrete[1]}"
    assert(len(ele_discrete) == nbins + 1), f"Length of discrete elevations should be nbins + 1: {nbins} but is {len(ele_discrete)}"
    assert(len(R_discrete) == nbins + 1), f"Length of discrete R values should be nbins + 1: {nbins} but is {len(ele_discrete)}"