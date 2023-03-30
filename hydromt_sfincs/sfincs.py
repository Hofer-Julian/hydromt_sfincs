# -*- coding: utf-8 -*-
from __future__ import annotations
import os
from os.path import join, isfile, abspath, dirname, basename, isabs
import glob
import numpy as np
import logging
import pyflwdir
import geopandas as gpd
import pandas as pd
from pyproj import CRS
import xarray as xr
from pathlib import Path
from typing import Dict, Tuple, List, Union, Any
from shapely.geometry import box

import hydromt
from hydromt.models.model_grid import GridModel
from hydromt.models.model_mesh import MeshMixin
from hydromt.vector import GeoDataset, GeoDataArray
from hydromt.raster import RasterDataset, RasterDataArray

from . import workflows, utils, plots, DATADIR
from .regulargrid import RegularGrid
from .sfincs_input import SfincsInput

__all__ = ["SfincsModel"]

logger = logging.getLogger(__name__)


class SfincsModel(MeshMixin, GridModel):
    # GLOBAL Static class variables that can be used by all methods within
    # SfincsModel class. Typically list of variables (e.g. _MAPS) or
    # dict with varname - filename pairs (e.g. thin_dams : thd)
    _NAME = "sfincs"
    _GEOMS = {
        "observation_points": "obs",
        "weirs": "weir",
        "thin_dams": "thd",
    }  # parsed to dict of geopandas.GeoDataFrame
    _FORCING_1D = {
        # timeseries (can be multiple), locations tuple
        "waterlevel": (["bzs"], "bnd"),
        "waves": (["bzi"], "bnd"),
        "discharge": (["dis"], "src"),
        "precip": (["precip"], None),
        "wavespectra": (["bhs", "btp", "bwd", "bds"], "bwv"),
        "wavemaker": (["whi", "wti", "wst"], "wvp"),  # TODO check names and test
    }
    _FORCING_NET = {
        # 2D forcing sfincs name, rename tuple
        "waterlevel": ("netbndbzsbzi", {"zs": "bzs", "zi": "bzi"}),
        "discharge": ("netsrcdis", {"discharge": "dis"}),
        "precip": ("netampr", {"Precipitation": "precip"}),
        "press": ("netamp", {"barometric_pressure": "press"}),
        "wind": ("netamuamv", {"eastward_wind": "wind_u", "northward_wind": "wind_v"}),
    }
    _FORCING_SPW = {"spiderweb": "spw"}  # TODO add read and write functions
    _MAPS = ["msk", "dep", "scs", "manning", "qinf"]
    _STATES = ["rst", "ini"]
    _FOLDERS = []
    _CLI_ARGS = {"region": "setup_grid_from_region", "res": "setup_grid_from_region"}
    _CONF = "sfincs.inp"
    _DATADIR = DATADIR
    _ATTRS = {
        "dep": {"standard_name": "elevation", "unit": "m+ref"},
        "msk": {"standard_name": "mask", "unit": "-"},
        "scs": {
            "standard_name": "potential maximum soil moisture retention",
            "unit": "in",
        },
        "qinf": {"standard_name": "infiltration rate", "unit": "mm.hr-1"},
        "manning": {"standard_name": "manning roughness", "unit": "s.m-1/3"},
        "bzs": {"standard_name": "waterlevel", "unit": "m+ref"},
        "bzi": {"standard_name": "wave height", "unit": "m"},
        "dis": {"standard_name": "discharge", "unit": "m3.s-1"},
        "precip": {"standard_name": "precipitation", "unit": "mm.hr-1"},
    }

    def __init__(
        self,
        root: str = None,
        mode: str = "w",
        config_fn: str = "sfincs.inp",
        write_gis: bool = True,
        data_libs: Union[List[str], str] = None,
        logger=logger,
    ):
        """
        The SFINCS model class (SfincsModel) contains methods to read, write, setup and edit
        `SFINCS <https://sfincs.readthedocs.io/en/latest/>`_ models.

        Parameters
        ----------
        root: str, Path, optional
            Path to model folder
        mode: {'w', 'r+', 'r'}
            Open model in write, append or reading mode, by default 'w'
        config_fn: str, Path, optional
            Filename of model config file, by default "sfincs.inp"
        write_gis: bool
            Write model files additionally to geotiff and geojson, by default True
        data_libs: List, str
            List of data catalog yaml files, by default None

        """
        # model folders
        self._write_gis = write_gis
        if write_gis and "gis" not in self._FOLDERS:
            self._FOLDERS.append("gis")

        super().__init__(
            root=root,
            mode=mode,
            config_fn=config_fn,
            data_libs=data_libs,
            logger=logger,
        )

        # placeholder grid classes
        self.grid_type = None
        self.reggrid = None
        self.quadtree = None
        self.subgrid = xr.Dataset()

    @property
    def mask(self) -> xr.DataArray | None:
        """Returns model mask"""
        if self.grid_type == "regular":
            if "msk" in self.grid:
                return self.grid["msk"]
            elif self.reggrid is not None:
                return self.reggrid.empty_mask

    @property
    def region(self) -> gpd.GeoDataFrame:
        """Returns the geometry of the active model cells."""
        # NOTE overwrites property in GridModel
        region = gpd.GeoDataFrame()
        if "region" in self.geoms:
            region = self.geoms["region"]
        elif "msk" in self.grid and np.any(self.grid["msk"] > 0):
            da = xr.where(self.mask > 0, 1, 0).astype(np.int16)
            da.raster.set_nodata(0)
            region = da.raster.vectorize().dissolve()
        elif self.reggrid is not None:
            region = self.reggrid.empty_mask.raster.box
        return region

    @property
    def crs(self) -> CRS | None:
        """Returns the model crs"""
        if self.grid_type == "regular":
            return self.reggrid.crs
        elif self.grid_type == "quadtree":
            return self.quadtree.crs

    def set_crs(self, crs: Any) -> None:
        """Sets the model crs"""
        if self.grid_type == "regular":
            self.reggrid.crs = CRS.from_user_input(crs)
            self.grid.raster.set_crs(self.reggrid.crs)
        elif self.grid_type == "quadtree":
            self.quadtree.crs = CRS.from_user_input(crs)

    def create_grid(
        self,
        x0: float,
        y0: float,
        dx: float,
        dy: float,
        mmax: int,
        nmax: int,
        rotation: float = None,
        epsg: int = None,
    ):
        """Creates a regular or quadtree grid.

        Parameters
        ----------
        x0, y0 : float
            x,y coordinates of the origin of the grid
        dx, dy : float
            grid cell size in x and y direction
        mmax, nmax : int
            number of grid cells in x and y direction
        rotation : float, optional
            rotation of grid [degree angle], by default None
        epsg : int, optional
            epsg-code of the coordinate reference system, by default None
        """
        self.config.update(
            x0=x0,
            y0=y0,
            dx=dx,
            dy=dy,
            nmax=nmax,
            mmax=mmax,
            rotation=rotation,
            epsg=epsg,
        )
        self.update_grid_from_config()

        # TODO gdf_refinement for quadtree

    def setup_grid(
        self,
        x0: float,
        y0: float,
        dx: float,
        dy: float,
        nmax: int,
        mmax: int,
        rotation: float,
        epsg: int,
    ):
        """Setup a regular or quadtree grid.

        Parameters
        ----------
        x0, y0 : float
            x,y coordinates of the origin of the grid
        dx, dy : float
            grid cell size in x and y direction
        mmax, nmax : int
            number of grid cells in x and y direction
        rotation : float, optional
            rotation of grid [degree angle], by default None
        epsg : int, optional
            epsg-code of the coordinate reference system, by default None

        See Also
        --------
        :py:meth:`SfincsModel.create_grid`
        """
        # for now the setup and create methods are identical, this will change when quadtree is implemented
        self.create_grid(
            x0=x0,
            y0=y0,
            dx=dx,
            dy=dy,
            nmax=nmax,
            mmax=mmax,
            rotation=rotation,
            epsg=epsg,
        )

    def create_grid_from_region(
        self,
        gdf_region: gpd.GeoDataFrame,
        res: float,
        minimum_rotated_rectangle: bool = False,
    ):
        """Creates a regular or quadtree grid from a region.

        Parameters
        ----------
        gdf_region : gpd.GeoDataFrame
            GeoDataFrame with a single polygon defining the region
        res : float
            grid resolution
        minimum_rotated_rectangle : bool, optional
            if True, a minimum rotated rectangular grid is fitted around the region, by default False
        """
        # TODO gdf_region.minimum_rotated_rectangle for rotated grid
        # https://stackoverflow.com/questions/66108528/angle-in-minimum-rotated-rectangle

        # NOTE keyword minimum_rotated_rectangle is added to still have the possibility to create unrotated grids if needed (e.g. for FEWS?)
        if minimum_rotated_rectangle:
            # assuming that is is a single geometry
            mrr = gdf_region.geometry.iloc[0].minimum_rotated_rectangle
            x0,y0,mmax,nmax,az = utils.rotated_grid(mrr,res)
            rotation = 90 - az
        else:
            west, south, east, north = gdf_region.total_bounds
            x0 = west
            y0 = south
            mmax = int(np.ceil((east - west) / res))
            nmax = int(np.ceil((north - south) / res))
            rotation = 0
        self.create_grid(
            x0=x0,
            y0=y0,
            dx=res,
            dy=res,
            nmax=nmax,
            mmax=mmax,
            rotation=rotation,  # Set rotation to 0 for grid based on region (see also TODO)
            epsg=gdf_region.crs.to_epsg(),
        )

    def setup_grid_from_region(
        self,
        region: dict,
        res: float = 100,
        crs: Union[str, int] = "utm",
        hydrography_fn: str = "merit_hydro",  # TODO: change to None
        basin_index_fn: str = "merit_hydro_index",  # TODO: change to None
    ):
        """Setup a regular or quadtree grid from a region.

        Parameters
        ----------
        region : dict
            Dictionary describing region of interest, e.g.:
            * {'bbox': [xmin, ymin, xmax, ymax]}
            * {'geom': 'path/to/polygon_geometry'}

            For a complete overview of all region options,
            see :py:function:~hydromt.workflows.basin_mask.parse_region
        res : float, optional
            grid resolution, by default 100 m
        crs : Union[str, int], optional
            coordinate reference system of the grid
            if "utm" (default) the best UTM zone is selected
            else a pyproj crs string or epsg code (int) can be provided
        grid_type : str, optional
            grid type, "regular" (default) or "quadtree"
        refinement_fn : str, optional
            Path or data source name of polygons where grid should be refined, only used when grid_type=quadtree, by default None
        hydrography_fn : str
            Name of data source for hydrography data.
        basin_index_fn : str
            Name of data source with basin (bounding box) geometries associated with
            the 'basins' layer of `hydrography_fn`. Only required if the `region` is
            based on a (sub)(inter)basins without a 'bounds' argument.

        See Also
        ---------
        :py:meth:`SfincsModel.create_grid_from_region`

        """
        # setup `region` of interest of the model.
        self.setup_region(
            region=region,
            hydrography_fn=hydrography_fn,
            basin_index_fn=basin_index_fn,
        )
        # get pyproj crs of best UTM zone if crs=utm
        pyproj_crs = hydromt.gis_utils.parse_crs(
            crs, self.region.to_crs(4326).total_bounds
        )
        if self.region.crs != pyproj_crs:
            self.geoms["region"] = self.geoms["region"].to_crs(pyproj_crs)

        # create grid from region
        self.create_grid_from_region(
            gdf_region=self.region,
            res=res,
        )

    def create_dep(
        self,
        datasets_dep: List[dict],
        buffer_cells: int = 0,  # not in list
        interp_method: str = "linear",  # used for buffer cells only
        logger=logger,
    ) -> xr.DataArray:
        """Interpolate topobathy (dep) data to the model grid

        Adds model grid layers:

        * **dep**: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing an xarray.DataSet and optional merge arguments e.g.:
            [{'da': merit_hydro_da, 'zmin': 0.01}, {'da': gebco_da, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        buffer_cells : int, optional
            Number of cells between datasets to ensure smooth transition of bed levels, by default 0
        interp_method : str, optional
            Interpolation method used to fill the buffer cells , by default "linear"

        Returns
        -------
        xr.DataArray
            Topobathy data on the model grid
        """

        if self.grid_type == "regular":
            da_dep = workflows.merge_multi_dataarrays(
                da_list=datasets_dep,
                da_like=self.mask,
                buffer_cells=buffer_cells,
                interp_method=interp_method,
                logger=logger,
            )

            # check if no nan data is present in the bed levels
            if not np.isnan(da_dep).any():
                self.logger.warning(
                    f"Interpolate data at {int(np.sum(np.isnan(da_dep.values)))} cells"
                )
                da_dep = da_dep.raster.interpolate_na(method="rio_idw")

            self.set_grid(da_dep, name="dep")
            # FIXME this shouldn't be necessary, since da_dep should already have the crs
            if self.crs is not None and self.grid.raster.crs is None:
                self.grid.set_crs(self.crs)

            if "depfile" not in self.config:
                self.config.update({"depfile": "sfincs.dep"})
        elif self.grid_type == "quadtree":
            raise NotImplementedError(
                "Create dep not yet implemented for quadtree grids."
            )

        return da_dep

    def setup_dep(
        self,
        datasets_dep: List[dict],
        buffer_cells: int = 0,  # not in list
        interp_method: str = "linear",  # used for buffer cells only
    ):
        """Interpolate topobathy (dep) data to the model grid.

        Adds model grid layers:

        * **dep**: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (dep_fn) and optional merge arguments e.g.:
            [{'dep_fn': merit_hydro, 'zmin': 0.01}, {'dep_fn': gebco, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        buffer_cells : int, optional
            Number of cells between datasets to ensure smooth transition of bed levels, by default 0
        interp_method : str, optional
            Interpolation method used to fill the buffer cells , by default "linear"  

        See Also
        ---------
        :py:meth:`SfincsModel.create_dep`      

        """

        # retrieve model resolution to determine zoom level for xyz-datasets
        # TODO fix for quadtree
        if not self.mask.raster.crs.is_geographic:
            res = np.abs(self.mask.raster.res[0])
        else:
            res = np.abs(self.mask.raster.res[0]) * 111111.0

        datasets_dep = self._parse_datasets_dep(datasets_dep, res=res)

        self.create_dep(
            datasets_dep=datasets_dep,
            buffer_cells=buffer_cells,
            interp_method=interp_method,
            logger=self.logger,
        )

    def create_mask_active(
        self,
        gdf_region: gpd.GeoDataFrame = None,
        gdf_include: gpd.GeoDataFrame = None,
        gdf_exclude: gpd.GeoDataFrame = None,
        zmin: float = None,
        zmax: float = None,
        fill_area: float = 10,
        drop_area: float = 0,
        connectivity: int = 8,
        all_touched=True,
        reset_mask=False,
    ) -> xr.DataArray:
        """Create an integer mask with inactive (msk=0) and active (msk=1) cells, optionally bounded
        by several criteria.

        Parameters
        ----------
        gdf_region: geopandas.GeoDataFrame, optional
            Geometry with area to initiliaze active mask with; proceding arguments can be used to include/exclude cells
            If not given, existing mask (if present) is used, else mask is initialized empty.
        gdf_include, gdf_exclude: geopandas.GeoDataFrame, optional
            Geometries with areas to include/exclude from the active model cells.
            Note that include (second last) and exclude (last) areas are processed after other critera,
            i.e. `zmin`, `zmax` and `drop_area`, and thus overrule these criteria for active model cells.
        zmin, zmax : float, optional
            Minimum and maximum elevation thresholds for active model cells.
        fill_area : float, optional
            Maximum area [km2] of contiguous cells below `zmin` or above `zmax` but surrounded
            by cells within the valid elevation range to be kept as active cells, by default 10 km2.
        drop_area : float, optional
            Maximum area [km2] of contiguous cells to be set as inactive cells, by default 0 km2.
        connectivity: {4, 8}
            The connectivity used to define contiguous cells, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_mask: bool, optional
            If True, reset existing mask layer. If False (default) updating existing mask.

        Returns
        -------
        da_mask: xr.DataArray
            Integer SFINCS model mask with inactive (msk=0), active (msk=1) cells
        """
        
        if self.grid_type == "regular":
            da_mask = self.reggrid.create_mask_active(
                da_mask=self.grid["msk"] if "msk" in self.grid else None,
                da_dep=self.grid["dep"] if "dep" in self.grid else None,
                gdf_region=gdf_region,
                gdf_include=gdf_include,
                gdf_exclude=gdf_exclude,
                zmin=zmin,
                zmax=zmax,
                fill_area=fill_area,
                drop_area=drop_area,
                connectivity=connectivity,
                all_touched=all_touched,
                reset_mask=reset_mask,
                # logger=self.logger,
            )
            self.set_grid(da_mask, name="msk")
            # update config
            if "mskfile" not in self.config:
                self.config.update({"mskfile": "sfincs.msk"})
            if "indexfile" not in self.config:
                self.config.update({"indexfile": "sfincs.ind"})
            # update region
            self.logger.info("Derive region geometry based on active cells.")
            region = da_mask.where(da_mask <= 1, 1).raster.vectorize()
            self.set_geoms(region, "region")

        return da_mask

    def setup_mask_active(
        self,
        region_fn=None,
        include_mask_fn=None,
        exclude_mask_fn=None,
        mask_buffer=0,
        zmin=None,
        zmax=None,
        fill_area=10,
        drop_area=0,
        connectivity=8,
        all_touched=True,
        reset_mask=False,
    ):
        """Setup method to create mask of active model cells.

        The SFINCS model mask defines inactive (msk=0), active (msk=1), and waterlevel boundary (msk=2)
        and outflow boundary (msk=3) cells. This method sets the active and inactive cells.

        Active model cells are based on a region and cells with valid elevation (i.e. not nodata),
        optionally bounded by areas inside the include geomtries, outside the exclude geomtries,
        larger or equal than a minimum elevation threshhold and smaller or equal than a
        maximum elevation threshhold.
        All conditions are combined using a logical AND operation.

        Sets model layers:

        * **msk** map: model mask [-]

        Parameters
        ----------
        region_fn: str, optional
            Path or data source name of polygons to initiliaze active mask with; proceding arguments can be used to include/exclude cells
            If not given, existing mask (if present) used, else mask is initialized empty.
        include_mask_fn, exclude_mask_fn: str, optional
            Path or data source name of polygons to include/exclude from the active model domain.
            Note that include (second last) and exclude (last) areas are processed after other critera,
            i.e. `zmin`, `zmax` and `drop_area`, and thus overrule these criteria for active model cells.
        mask_buffer: float, optional
            If larger than zero, extend the `include_mask` geometry with a buffer [m],
            by default 0.
        zmin, zmax : float, optional
            Minimum and maximum elevation thresholds for active model cells.
        fill_area : float, optional
            Maximum area [km2] of contiguous cells below `zmin` or above `zmax` but surrounded
            by cells within the valid elevation range to be kept as active cells, by default 10 km2.
        drop_area : float, optional
            Maximum area [km2] of contiguous cells to be set as inactive cells, by default 0 km2.
        connectivity, {4, 8}:
            The connectivity used to define contiguous cells, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_mask: bool, optional
            If True, reset existing mask layer. If False (default) updating existing mask.

        See Also
        ----------
        :py:meth:`SfincsModel.create_mask_active`

        """

        # read geometries
        gdf0, gdf1, gdf2 = None, None, None
        bbox = self.region.to_crs(4326).total_bounds
        if region_fn is not None:
            if str(region_fn).endswith(".pol"):
                # NOTE polygons should be in same CRS as model
                gdf0 = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=region_fn), crs=self.region.crs
                )
            else:
                gdf1 = self.data_catalog.get_geodataframe(region_fn, bbox=bbox)
            if mask_buffer > 0:  # NOTE assumes model in projected CRS!
                gdf1["geometry"] = gdf1.to_crs(self.crs).buffer(mask_buffer)
        if include_mask_fn is not None:
            if str(include_mask_fn).endswith(".pol"):
                # NOTE polygons should be in same CRS as model
                gdf1 = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=include_mask_fn), crs=self.region.crs
                )
            else:
                gdf1 = self.data_catalog.get_geodataframe(include_mask_fn, bbox=bbox)
        if exclude_mask_fn is not None:
            if str(exclude_mask_fn).endswith(".pol"):
                gdf2 = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=exclude_mask_fn), crs=self.region.crs
                )
            else:
                gdf2 = self.data_catalog.get_geodataframe(exclude_mask_fn, bbox=bbox)

        # get mask
        da_mask = self.create_mask_active(
            gdf_region=gdf0,
            gdf_include=gdf1,
            gdf_exclude=gdf2,
            zmin=zmin,
            zmax=zmax,
            fill_area=fill_area,
            drop_area=drop_area,
            connectivity=connectivity,
            all_touched=all_touched,
            reset_mask=reset_mask,
            # logger=self.logger,
        )

        self.logger.debug("Derive region geometry based on active cells.")
        region = da_mask.where(da_mask <= 1, 1).raster.vectorize()
        self.set_geoms(region, "region")

    def create_mask_bounds(
        self,
        btype: str = "waterlevel",
        gdf_include: gpd.GeoDataFrame = None,
        gdf_exclude: gpd.GeoDataFrame = None,
        zmin: float = None,
        zmax: float = None,
        connectivity: int = 8,
        all_touched: bool = False,
        reset_bounds: bool = False,
    ) -> xr.DataArray:
        """Returns an integer SFINCS model mask with inactive (msk=0), active (msk=1), and waterlevel boundary (msk=2)
            and outflow boundary (msk=3) cells.  Boundary cells are defined by cells at the edge of active model domain.

        Parameters
        ----------
        btype: {'waterlevel', 'outflow'}
            Boundary type
        gdf_include, gdf_exclude: geopandas.GeoDataFrame
            Geometries with areas to include/exclude from the model boundary.
        zmin, zmax : float, optional
            Minimum and maximum elevation thresholds for boundary cells.             
            Note that when include and exclude areas are used, the elevation range is only applied
            on cells within the include area and outside the exclude area.
        connectivity: {4, 8}
            The connectivity used to detect the model edge, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_bounds: bool, optional
            If True, reset existing boundary cells of the selected boundary
            type (`btype`) before setting new boundary cells, by default False.

        Returns
        -------
        da_mask: xr.DataArray
            Integer SFINCS model mask with inactive (msk=0), active (msk=1), and waterlevel boundary (msk=2)
            and outflow boundary (msk=3) cells
            
        """

        if self.grid_type == "regular":
            da_mask = self.reggrid.create_mask_bounds(
                da_mask=self.grid["msk"],
                btype=btype,
                gdf_include=gdf_include,
                gdf_exclude=gdf_exclude,
                da_dep=self.grid["dep"] if "dep" in self.grid else None,
                zmin=zmin,
                zmax=zmax,
                connectivity=connectivity,
                all_touched=all_touched,
                reset_bounds=reset_bounds,
            )
            self.set_grid(da_mask, name="msk")

        return da_mask

    def setup_mask_bounds(
        self,
        btype="waterlevel",
        include_fn=None,
        exclude_fn=None,
        zmin=None,
        zmax=None,
        connectivity=8,
        reset_bounds=False,
    ):
        """Set boundary cells in the model mask.

        The SFINCS model mask defines inactive (msk=0), active (msk=1), and waterlevel boundary (msk=2)
        and outflow boundary (msk=3) cells. Active cells set using the `setup_mask` method,
        while this method sets both types of boundary cells, see `btype` argument.

        Boundary cells at the edge of the active model domain,
        optionally bounded by areas inside the include geomtries, outside the exclude geomtries,
        larger or equal than a minimum elevation threshhold and smaller or equal than a
        maximum elevation threshhold.
        All conditions are combined using a logical AND operation.

        Updates model layers:

        * **msk** map: model mask [-]

        Parameters
        ----------
        btype: {'waterlevel', 'outflow'}
            Boundary type
        include_mask_fn, exclude_mask_fn: str, optional
            Path or data source name for geometries with areas to include/exclude from the model boundary.
        zmin, zmax : float, optional
            Minimum and maximum elevation thresholds for boundary cells.
            Note that when include and exclude areas are used, the elevation range is only applied
            on cells within the include area and outside the exclude area.
        reset_bounds: bool, optional
            If True, reset existing boundary cells of the selected boundary
            type (`btype`) before setting new boundary cells, by default False.
        connectivity, {4, 8}:
            The connectivity used to detect the model edge, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        """

        # get include / exclude geometries
        gdf_include, gdf_exclude = None, None
        bbox = self.region.to_crs(4326).total_bounds

        if include_fn:
            if str(include_fn).endswith(".pol"):
                gdf_include = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=include_fn), crs=self.region.crs
                )
            else:
                gdf_include = self.data_catalog.get_geodataframe(include_fn, bbox=bbox)
        if exclude_fn:
            if str(exclude_fn).endswith(".pol"):
                gdf_exclude = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=exclude_fn), crs=self.region.crs
                )
            else:
                gdf_exclude = self.data_catalog.get_geodataframe(exclude_fn, bbox=bbox)

        # mask values
        da_mask = self.create_mask_bounds(
            btype=btype,
            gdf_include=gdf_include,
            gdf_exclude=gdf_exclude,
            zmin=zmin,
            zmax=zmax,
            connectivity=connectivity,
            reset_bounds=reset_bounds,
        )

        self.set_grid(da_mask, "msk")

    def create_subgrid(
        self,
        datasets_dep: List[dict],
        datasets_rgh: List[dict] = [],
        buffer_cells: int = 0,
        nbins: int = 10,
        nr_subgrid_pixels: int = 20,
        nrmax: int = 2000,  # blocksize
        max_gradient: float = 5.0,
        z_minimum: float = -99999.0,
        manning_land: float = 0.04,
        manning_sea: float = 0.02,
        rgh_lev_land: float = 0.0,
        make_dep_tiles: bool = False,
        make_manning_tiles: bool = False,
    ) -> xr.Dataset:
        """Create subgrid tables based on a list of depth and Manning's n datasets.

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing an xarray.DataSet and optional merge arguments e.g.:
            [{'da': merit_hydro_da, 'zmin': 0.01}, {'da': gebco_da, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        datsets_rgh : List[dict], optional
            List of dictionaries with Manning's n data, each containing an xarray.DataSet with manning values and optional merge arguments
        buffer_cells : int, optional
            Number of cells between datasets to ensure smooth transition of bed levels, by default 0
        nbins : int, optional
            Number of bins in the subgrid tables, by default 10
        nr_subgrid_pixels : int, optional
            Number of subgrid pixels per computational cell, by default 20
        nrmax : int, optional
            Maximum number of cells per subgrid-block, by default 2000
            These blocks are used to prevent memory issues while working with large datasets
        max_gradient : float, optional
            Maximum gradient in the subgrid tables, by default 5.0
        z_minimum : float, optional
            Minimum depth in the subgrid tables, by default -99999.0
        manning_land, manning_sea : float, optional
            Constant manning roughness values for land and sea, by default 0.04 and 0.02 s.m-1/3
            Note that these values are only used when no Manning's n datasets are provided, or to fill the nodata values
        rgh_lev_land : float, optional
            Elevation level to distinguish land and sea roughness (when using manning_land and manning_sea), by default 0.0
        make_dep_tiles : bool, optional
            Create geotiff of the merged topobathy on the subgrid resolution, by default False
        make_rgh_tiles : bool, optional
            Create geotiff of the merged roughness on the subgrid resolution, by default False

        Returns
        -------
        xr.Dataset
            Subgrid tables for the SFINCS domain containing the variables: 
            ["z_zmin", "z_zmax", "z_zmin", "z_zmean", "z_volmax",
            "u_zmin", "u_zmax", "v_zmin", "v_zmax","z_depth",
            "u_hrep", "u_navg", "v_hrep", "v_navg"]
        """        
        # folder where high-resolution topobathy and manning geotiffs are stored
        if make_dep_tiles or make_manning_tiles:
            highres_dir = os.path.join(self.root, "tiles", "subgrid")
            if not os.path.isdir(highres_dir):
                os.makedirs(highres_dir)
        else:
            highres_dir = None

        if self.grid_type == "regular":
            self.reggrid.subgrid.build(
                da_mask=self.mask,
                datasets_dep=datasets_dep,
                datasets_rgh=datasets_rgh,
                buffer_cells=buffer_cells,
                nbins=nbins,
                nr_subgrid_pixels=nr_subgrid_pixels,
                nrmax=nrmax,
                max_gradient=max_gradient,
                z_minimum=z_minimum,
                manning_land=manning_land,
                manning_sea=manning_sea,
                rgh_lev_land=rgh_lev_land,
                make_dep_tiles=make_dep_tiles,
                make_manning_tiles=make_manning_tiles,
                highres_dir=highres_dir,
            )
            self.subgrid = self.reggrid.subgrid.to_xarray(
                dims=self.mask.raster.dims, coords=self.mask.raster.coords
            )
        elif self.grid_type == "quadtree":
            pass

        if "sbgfile" not in self.config:  # only add sbgfile if not already present
            self.config.update({"sbgfile": "sfincs.sbg"})
        # subgrid is used so no depfile or manningfile needed
        if "depfile" in self.config:
            self.config.pop("depfile")  # remove depfile from config
        if "manningfile" in self.config:
            self.config.pop("manningfile")  # remove manningfile from config

    def setup_subgrid(
        self,
        datasets_dep: List[dict],
        datasets_rgh: List[dict] = [],
        buffer_cells: int = 0,
        nbins: int = 10,
        nr_subgrid_pixels: int = 20,
        nrmax: int = 2000,  # blocksize
        max_gradient: float = 5.0,
        z_minimum: float = -99999.0,
        manning_land: float = 0.04,
        manning_sea: float = 0.02,
        rgh_lev_land: float = 0.0,
        make_dep_tiles: bool = False,
        make_manning_tiles: bool = False,
    ):
        """Setup method for subgrid tables based on a list of depth and Manning's n datasets. 

        These datasets are used to derive relations between the water level and the volume in a cell to do the continuity update,
        and a representative water depth used to calculate momentum fluxes. 
        
        This allows that one can compute on a coarser computational grid, while still accounting for the local topography and roughness.

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (dep_fn) and optional merge arguments e.g.:
            [{'dep_fn': merit_hydro, 'zmin': 0.01}, {'dep_fn': gebco, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        datasets_rgh : List[dict], optional
            List of dictionaries with Manning's n datasets. Each dictionary should at least contain one of the following:
            * (1) manning_fn: filename (or Path) of gridded data with manning values
            * (2) lulc_fn (and map_fn) :a combination of a filename of gridded landuse/landcover and a mapping table.
            In additon, optional merge arguments can be provided e.g.: merge_method, gdf_valid_fn 
        buffer_cells : int, optional
            Number of cells between datasets to ensure smooth transition of bed levels, by default 0
        nbins : int, optional
            Number of bins in the subgrid tables, by default 10
        nr_subgrid_pixels : int, optional
            Number of subgrid pixels per computational cell, by default 20
        nrmax : int, optional
            Maximum number of cells per subgrid-block, by default 2000
            These blocks are used to prevent memory issues while working with large datasets
        max_gradient : float, optional
            Maximum gradient in the subgrid tables, by default 5.0
        z_minimum : float, optional
            Minimum depth in the subgrid tables, by default -99999.0
        manning_land, manning_sea : float, optional
            Constant manning roughness values for land and sea, by default 0.04 and 0.02 s.m-1/3
            Note that these values are only used when no Manning's n datasets are provided, or to fill the nodata values
        rgh_lev_land : float, optional
            Elevation level to distinguish land and sea roughness (when using manning_land and manning_sea), by default 0.0
        make_dep_tiles : bool, optional
            Create geotiff of the merged topobathy on the subgrid resolution, by default False
        make_rgh_tiles : bool, optional
            Create geotiff of the merged roughness on the subgrid resolution, by default False

        See Also
        --------
        :py:meth:'SfincsModel.create_subgrid'

        """	

        # retrieve model resolution
        # TODO fix for quadtree
        if not self.mask.raster.crs.is_geographic:
            res = np.abs(self.mask.raster.res[0]) / nr_subgrid_pixels
        else:
            res = np.abs(self.mask.raster.res[0]) * 111111.0 / nr_subgrid_pixels

        datasets_dep = self._parse_datasets_dep(datasets_dep, res=res)

        if len(datasets_rgh) > 0:
            # NOTE conversion from landuse/landcover to manning happens here
            datasets_rgh = self._parse_datasets_rgh(datasets_rgh)
        else:
            datasets_rgh = []

        self.create_subgrid(
            datasets_dep=datasets_dep,
            datasets_rgh=datasets_rgh,
            buffer_cells=buffer_cells,
            nbins=nbins,
            nr_subgrid_pixels=nr_subgrid_pixels,
            nrmax=nrmax,
            max_gradient=max_gradient,
            z_minimum=z_minimum,
            manning_land=manning_land,
            manning_sea=manning_sea,
            rgh_lev_land=rgh_lev_land,
            make_dep_tiles=make_dep_tiles,
            make_manning_tiles=make_manning_tiles,
        )

    def setup_river_hydrography(self, hydrography_fn=None, adjust_dem=False, **kwargs):
        """Setup hydrography layers for flow directions ("flwdir") and upstream area
        ("uparea") which are required to setup the setup_river* model components.

        If no hydrography data is provided (`hydrography_fn=None`) flow directions are
        derived from the model elevation data.
        Note that in that case the upstream area map will miss the contribution from area
        upstream of the model domain and incoming rivers in the `setup_river_inflow`
        cannot be detected.

        If the model crs or resolution is different from the input hydrography data,
        it is reprojected to the model grid. Note that this works best if the destination
        resolution is roughly the same or higher (i.e. smaller cells).

        Adds model layers (both not used by SFINCS!):

        * **uparea** map: upstream area [km2]
        * **flwdir** map: local D8 flow directions [-]

        Updates model layer (if `adjust_dem=True`):

        * **dep** map: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        hydrography_fn : str
            Path or data source name for hydrography raster data, by default None
            and derived from model elevation data.

            * Required variable: ['uparea']
            * Optional variable: ['flwdir']
        adjust_dem: bool, optional
            Adjust the model elevation such that each downstream cell is at the
            same or lower elevation. By default True.
        """
        name = "dep"
        assert name in self.grid
        da_elv = self.grid[name]

        da_elv = (
            da_elv.raster.clip_geom(self.region, mask=True)
            .raster.mask_nodata()
            .fillna(-9999)  # force nodata value to be -9999
            .round(2)  # cm precision
        )
        da_elv.raster.set_nodata(-9999)

        # check N->S orientation
        if da_elv.raster.res[1] > 0:
            da_elv = da_elv.raster.flipud()

        if hydrography_fn is not None:
            ds_hydro = self.data_catalog.get_rasterdataset(
                hydrography_fn,
                geom=self.grid.raster.box,
                buffer=20,
                single_var_as_array=False,
            )
            assert "uparea" in ds_hydro
            warp = da_elv.raster.aligned_grid(ds_hydro)
            if not warp or "flwdir" not in ds_hydro:
                self.logger.info("Reprojecting hydrography data to destination grid.")
                ds_out = hydromt.flw.reproject_hydrography_like(
                    ds_hydro, da_elv, logger=self.logger, **kwargs
                )
            else:
                ds_out = ds_hydro[["uparea", "flwdir"]].raster.clip_bbox(
                    da_elv.raster.bounds
                )
            ds_out = ds_out.raster.mask(da_elv != da_elv.raster.nodata)
        else:
            self.logger.info("Getting hydrography data from model grid.")
            da_flw = hydromt.flw.d8_from_dem(da_elv, **kwargs)
            flwdir = hydromt.flw.flwdir_from_da(da_flw, ftype="d8")
            da_upa = xr.DataArray(
                dims=da_elv.raster.dims,
                data=flwdir.upstream_area(unit="km2"),
                name="uparea",
            )
            da_upa.raster.set_nodata(-9999)
            ds_out = xr.merge([da_flw, da_upa.reset_coords(drop=True)])

        self.logger.info("Saving hydrography data to grid.")
        self.set_grid(ds_out["uparea"])
        self.set_grid(ds_out["flwdir"])

        if adjust_dem:
            self.logger.info(f"Hydrologically adjusting {name} map.")
            flwdir = hydromt.flw.flwdir_from_da(ds_out["flwdir"], ftype="d8")
            da_elv.data = flwdir.dem_adjust(da_elv.values)
            self.set_grid(da_elv.round(2), name)

    def setup_river_bathymetry(
        self,
        river_geom_fn=None,
        river_mask_fn=None,
        qbankfull_fn=None,
        rivdph_method="gvf",
        rivwth_method="geom",
        river_upa=25.0,
        river_len=1000,
        min_rivwth=50.0,
        min_rivdph=1.0,
        rivbank=True,
        rivbankq=25,
        segment_length=3e3,
        smooth_length=10e3,
        constrain_rivbed=True,
        constrain_estuary=True,
        dig_river_d4=True,
        plot_riv_profiles=0,
        **kwargs,  # for workflows.get_river_bathymetry method
    ):
        """Burn rivers into the model elevation (dep) file.

        NOTE: this method is experimental and may change in the near future.

        River cells are based on the `river_mask_fn` raster file if `rivwth_method='mask'`,
        or if `rivwth_method='geom'` the rasterized segments buffered with half a river width
        ("rivwth" [m]) if that attribute is found in `river_geom_fn`.

        If a river segment geometry file `river_geom_fn` with bedlevel column ("zb" [m+REF]) or
        a river depth ("rivdph" [m]) in combination with `rivdph_method='geom'` is provided,
        this attribute is used directly.

        Otherwise, a river depth is estimated based on bankfull discharge ("qbankfull" [m3/s])
        attribute taken from the nearest river segment in `river_geom_fn` or `qbankfull_fn`
        upstream river boundary points if provided.

        The river depth is relative to the bankfull elevation profile if `rivbank=True` (default),
        which is estimated as the `rivbankq` elevation percentile [0-100] of cells neighboring river cells.
        This option requires the flow direction ("flwdir") and upstream area ("uparea") maps to be set
        using the "setup_river_hydrography" method. If `rivbank=False` the depth is simply subtracted
        from the elevation of river cells.

        Missing river width and river depth values are filled by propagating valid values downstream and
        using the constant minimum values `min_rivwth` and `min_rivdph` for the remaining missing values.

        Updates model layer:

        * **dep** map: combined elevation/bathymetry [m+ref]

        Adds model layers

        * **rivmsk** map: map of river cells (not used by SFINCS)
        * **rivers** geom: geometry of rivers (not used by SFINCS)

        Parameters
        ----------
        river_geom_fn : str, optional
            Line geometry with river attribute data.

            * Required variable for direct bed level burning: ['zb']
            * Required variable for direct river depth burning: ['rivdph'] (only in combination with rivdph_method='geom')
            * Variables used for river depth estimates: ['qbankfull', 'rivwth']

        river_mask_fn : str, optional
            River mask raster used to define river cells
        qbankfull_fn: str, optional
            Point geometry with bankfull discharge estimates

            * Required variable: ['qbankfull']

        rivdph_method : {'gvf', 'manning', 'powlaw'}
            River depth estimate method, by default 'gvf'
        rivwth_method : {'geom', 'mask'}
            Derive the river width from either the `river_geom_fn` (geom) or
            `river_mask_fn` (mask; default) data.
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 25.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1000 m.
        min_rivwth, min_rivdph: float, optional
            Minimum river width [m] (by default 50.0) and depth [m] (by default 1.0)
        rivbank: bool, optional
            If True (default), approximate the reference elevation for the river depth based
            on the river bankfull elevation at cells neighboring river cells. Otherwise
            use the elevation of the local river cell as reference level.
        rivbankq : float, optional
            quantile [1-100] for river bank estimation, by default 25
        segment_length : float, optional
            Approximate river segment length [m], by default 5e3
        smooth_length : float, optional
            Approximate smoothing length [m], by default 10e3
        constrain_estuary : bool, optional
            If True (default) fix the river depth in estuaries based on the upstream river depth.
        constrain_rivbed : bool, optional
            If True (default) correct the river bed level to be hydrologically correct,
            i.e. sloping downward in downstream direction.
        dig_river_d4: bool, optional
            If True (default), dig the river out to be hydrologically connected in D4.
        """
        if river_mask_fn is None and rivwth_method == "mask":
            raise ValueError(
                '"river_mask_fn" should be provided if rivwth_method="mask".'
            )
        # get basemap river flwdir
        assert "msk" in self.grid  # make sure msk is grid

        if self.grid.raster.res[1] > 0:
            ds = self.grid.raster.flipud()
        else:
            ds = self.grid

        flwdir = None
        if "flwdir" in ds:
            flwdir = hydromt.flw.flwdir_from_da(ds["flwdir"], mask=False)

        # read river line geometry data
        gdf_riv = None
        if river_geom_fn is not None:
            gdf_riv = self.data_catalog.get_geodataframe(
                river_geom_fn, geom=self.region
            ).to_crs(self.crs)
        # read river bankfull point data
        gdf_qbf = None
        if qbankfull_fn is not None:
            gdf_qbf = self.data_catalog.get_geodataframe(
                qbankfull_fn,
                geom=self.region,
            ).to_crs(self.crs)
        # read river mask raster data
        da_rivmask = None
        if river_mask_fn is not None:
            da_rivmask = self.data_catalog.get_rasterdataset(
                river_mask_fn, geom=self.region
            ).raster.reproject_like(ds, "max")
            ds["rivmsk"] = da_rivmask.where(ds["msk"] != 0, 0) != 0
        elif "rivmsk" in ds:
            self.logger.info(
                'River mask based on internal "rivmsk" layer. If this is unwanted '
                "delete the gis/rivmsk.tif file or drop the rivmsk grid variable."
            )

        # estimate elevation bed level based on qbankfull (and other parameters)
        if not (gdf_riv is not None and "zb" in gdf_riv):
            if flwdir is None:
                msg = '"flwdir" staticmap layer missing, run "setup_river_hydrography".'
                raise ValueError(msg)
            gdf_riv, ds["rivmsk"] = workflows.get_river_bathymetry(
                ds,
                flwdir=flwdir,
                gdf_riv=gdf_riv,
                gdf_qbf=gdf_qbf,
                rivdph_method=rivdph_method,
                rivwth_method=rivwth_method,
                river_upa=river_upa,
                river_len=river_len,
                min_rivdph=min_rivdph,
                min_rivwth=min_rivwth,
                rivbank=rivbank,
                rivbankq=rivbankq,
                segment_length=segment_length,
                smooth_length=smooth_length,
                elevtn_name="dep",
                constrain_estuary=constrain_estuary,
                constrain_rivbed=constrain_rivbed,
                logger=self.logger,
                **kwargs,
            )
        elif "rivmsk" not in ds:
            buffer = gdf_riv["rivwth"].values if "rivwth" in gdf_riv else 0
            gdf_riv_buf = gdf_riv.buffer(buffer)
            ds["rivmsk"] = ds.raster.geometry_mask(gdf_riv_buf, all_touched=True)

        # set elevation bed level
        da_elv1, ds["rivmsk"] = workflows.burn_river_zb(
            gdf_riv=gdf_riv,
            da_elv=ds["dep"],
            da_msk=ds["rivmsk"],
            flwdir=flwdir,
            river_d4=dig_river_d4,
            logger=self.logger,
        )

        if plot_riv_profiles > 0:
            # TODO move to plots
            import matplotlib.pyplot as plt

            flw = pyflwdir.from_dataframe(gdf_riv.set_index("idx"))
            upa_pit = gdf_riv.loc[flw.idxs_pit, "uparea"]
            n = int(plot_riv_profiles)
            idxs = flw.idxs_pit[np.argsort(upa_pit).values[::-1]][:n]
            paths, _ = flw.path(idxs=idxs, direction="up")
            _, axes = plt.subplots(n, 1, figsize=(7, n * 4))
            for path, ax in zip(paths, axes):
                g0 = gdf_riv.loc[path, :]
                x = g0["rivdst"].values
                ax.plot(x, g0["zs"], "--k", label="bankfull")
                ax.plot(x, g0["elevtn"], ":k", label="original zb")
                ax.plot(x, g0["zb"], "--g", label=f"{rivdph_method} zb (corrected)")
                mask = da_elv1.raster.geometry_mask(g0).values
                x1 = flwdir.distnc[mask]
                y1 = da_elv1.data[mask]
                s1 = np.argsort(x1)
                ax.plot(x1[s1], y1[s1], ".b", ms=2, label="zb (burned)")
            ax.legend()
            if not os.path.isdir(join(self.root, "figs")):
                os.makedirs(join(self.root, "figs"))
            fn_fig = join(self.root, "figs", "river_bathymetry.png")
            plt.savefig(fn_fig, dpi=225, bbox_inches="tight")

        # update dep
        self.set_grid(da_elv1.round(2), name="dep")
        # keep river geom and rivmsk for postprocessing
        self.set_geoms(gdf_riv, name="rivers")
        # save rivmask as int8 map (geotif does not support bool maps)
        da_rivmask = ds["rivmsk"].astype(np.int8).where(ds["msk"] > 0, 255)
        da_rivmask.raster.set_nodata(255)
        self.set_grid(da_rivmask, name="rivmsk")

    def setup_river_inflow(
        self,
        hydrography_fn=None,
        river_upa=25.0,
        river_len=1e3,
        river_width=2e3,
        keep_rivers_geom=False,
        buffer=10,
        **kwargs,  # catch deprecated args
    ):
        """Setup river inflow (source) points where a river enters the model domain.

        NOTE: this method requires the either `hydrography_fn` or `setup_river_hydrography` to be run first.
        NOTE: best to run after `setup_mask`

        Adds model layers:

        * **src** geoms: discharge boundary point locations
        * **dis** forcing: dummy discharge timeseries
        * **mask** map: SFINCS mask layer (only if `river_width` > 0)
        * **rivers_in** geoms: river centerline (if `keep_rivers_geom`; not used by SFINCS)

        Parameters
        ----------
        hydrography_fn: str, Path, optional
            Path or data source name for hydrography raster data, by default 'merit_hydro'.

            * Required layers: ['uparea', 'flwdir'].
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 25.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1 km.
        river_width: float, optional
            Estimated constant width [m] of the inflowing river. Boundary cells within
            half the width are forced to be closed (mask = 1) to avoid instabilities with
            nearby open or waterlevel boundary cells, by default 1 km.
        keep_rivers_geom: bool, optional
            If True, keep a geometry of the rivers "rivers_in" in geoms. By default False.
        buffer: int, optional
            Buffer [no. of cells] around model domain, by default 10.
        """
        if "basemaps_fn" in kwargs:
            self.logger.warning(
                "'basemaps_fn' is deprecated use 'hydrography_fn' instead."
            )
            hydrography_fn = kwargs.pop("basemaps_fn")

        if hydrography_fn is not None:
            ds = self.data_catalog.get_rasterdataset(
                hydrography_fn,
                geom=self.region,
                variables=["uparea", "flwdir"],
                buffer=buffer,
            )
        else:
            if self.grid.raster.res[1] > 0:
                ds = self.grid.raster.flipud()
            else:
                ds = self.grid
            if "uparea" not in ds or "flwdir" not in ds:
                raise ValueError(
                    '"uparea" and/or "flwdir" layers missing. '
                    "Run setup_river_hydrography first or provide hydrography_fn dataset."
                )

        # (re)calculate region to make sure it's accurate
        region = self.mask.where(self.mask <= 1, 1).raster.vectorize()
        gdf_src, gdf_riv = workflows.river_boundary_points(
            da_flwdir=ds["flwdir"],
            da_uparea=ds["uparea"],
            region=region,
            river_len=river_len,
            river_upa=river_upa,
            btype="inflow",
            return_river=keep_rivers_geom,
            logger=self.logger,
        )
        if len(gdf_src.index) == 0:
            return

        # set forcing with dummy timeseries to keep valid sfincs model
        gdf_src = gdf_src.to_crs(self.crs.to_epsg())
        self.set_forcing_1d(gdf_locs=gdf_src, name="dis")

        # set river
        if keep_rivers_geom and gdf_riv is not None:
            gdf_riv = gdf_riv.to_crs(self.crs.to_epsg())
            gdf_riv.index = gdf_riv.index.values + 1  # one based index
            self.set_geoms(gdf_riv, name="rivers_in")

        # update mask if closed_bounds_buffer > 0
        if river_width > 0:
            # apply buffer
            gdf_src_buf = gpd.GeoDataFrame(
                geometry=gdf_src.buffer(river_width / 2), crs=gdf_src.crs
            )
            # find intersect of buffer and model grid
            bounds = utils.mask_bounds(self.mask, gdf_mask=gdf_src_buf)
            # update model mask
            n = np.count_nonzero(bounds.values)
            if n > 0:
                da_mask = self.mask.where(~bounds, np.uint8(1))
                self.set_grid(da_mask, "msk")
                self.logger.debug(
                    f"{n:d} closed (mask=1) boundary cells set around src points."
                )

    def setup_river_outflow(
        self,
        hydrography_fn=None,
        river_upa=25.0,
        river_len=1e3,
        river_width=2e3,
        append_bounds=False,
        keep_rivers_geom=False,
        **kwargs,  # catch deprecated arguments
    ):
        """Setup open boundary cells (mask=3) where a river flows out of the model domain.

        Outflow locations are based on a minimal upstream area threshold. Locations within
        half `river_width` of a discharge source point or waterlevel boundary cells are omitted.

        NOTE: this method requires the either `hydrography_fn` input or `setup_river_hydrography` to be run first.
        NOTE: best to run after `setup_mask`, `setup_bounds` and `setup_river_inflow`

        Adds / edits model layers:

        * **msk** map: edited by adding outflow points (msk=3)
        * **river_out** geoms: river centerline (if `keep_rivers_geom`; not used by SFINCS)

        Parameters
        ----------
        hydrography_fn: str, Path, optional
            Path or data source name for hydrography raster data, by default 'merit_hydro'.
            * Required layers: ['uparea', 'flwdir'].
        river_width: int, optional
            The width [m] of the open boundary cells in the SFINCS msk file.
            By default 2km, i.e.: 1km to each side of the outflow location.
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 25.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1000 m.
        append_bounds: bool, optional
            If True, write new outflow boundary cells on top of existing. If False (default),
            first reset existing outflow boundary cells to normal active cells.
        keep_rivers_geom: bool, optional
            If True, keep a geometry of the rivers "rivers_out" in geoms. By default False.
        """
        if "outflow_width" in kwargs:
            self.logger.warning(
                "'outflow_width' is deprecated use 'river_width' instead."
            )
            river_width = kwargs.pop("outflow_width")
        if "basemaps_fn" in kwargs:
            self.logger.warning(
                "'basemaps_fn' is deprecated use 'hydrography_fn' instead."
            )
            hydrography_fn = kwargs.pop("basemaps_fn")

        if hydrography_fn is not None:
            ds = self.data_catalog.get_rasterdataset(
                hydrography_fn,
                geom=self.region,
                variables=["uparea", "flwdir"],
                buffer=10,
            )
        else:
            if self.grid.raster.res[1] > 0:
                ds = self.grid.raster.flipud()
            else:
                ds = self.grid
            if "uparea" not in ds or "flwdir" not in ds:
                raise ValueError(
                    '"uparea" and/or "flwdir" layers missing. '
                    "Run setup_river_hydrography first or provide hydrography_fn dataset."
                )

        # (re)calculate region to make sure it's accurate
        region = self.mask.where(self.mask <= 1, 1).raster.vectorize()
        gdf_out, gdf_riv = workflows.river_boundary_points(
            da_flwdir=ds["flwdir"],
            da_uparea=ds["uparea"],
            region=region,
            river_len=river_len,
            river_upa=river_upa,
            btype="outflow",
            return_river=keep_rivers_geom,
            logger=self.logger,
        )
        if len(gdf_out.index) == 0:
            return

        # apply buffer
        gdf_out = gdf_out.to_crs(self.crs.to_epsg())  # assumes projected CRS
        gdf_out_buf = gpd.GeoDataFrame(
            geometry=gdf_out.buffer(river_width / 2.0), crs=gdf_out.crs
        )
        # remove points near waterlevel boundary cells
        da_mask = self.mask
        msk2 = (da_mask == 2).astype(np.int8)
        msk_wdw = msk2.raster.zonal_stats(gdf_out_buf, stats="max")
        bool_drop = (msk_wdw[f"{da_mask.name}_max"] == 1).values
        if np.any(bool_drop):
            self.logger.debug(
                f"{int(sum(bool_drop)):d} outflow (mask=3) boundary cells near water level (mask=2) boundary cells dropped."
            )
            gdf_out = gdf_out[~bool_drop]
        if len(gdf_out.index) == 0:
            self.logger.debug(f"0 outflow (mask=3) boundary cells set.")
            return
        # remove outflow points near source points
        fname = self._FORCING_1D["discharge"][0]
        if fname in self.forcing:
            gdf_src = self.forcing[fname].vector.to_gdf()
            idx_drop = gpd.sjoin(gdf_out_buf, gdf_src, how="inner").index.values
            if idx_drop.size > 0:
                gdf_out_buf = gdf_out_buf.drop(idx_drop)
                self.logger.debug(
                    f"{idx_drop.size:d} outflow (mask=3) boundary cells near src points dropped."
                )

        # find intersect of buffer and model grid
        bounds = utils.mask_bounds(da_mask, gdf_mask=gdf_out_buf)
        # update model mask
        if not append_bounds:  # reset existing outflow boundary cells
            da_mask = da_mask.where(da_mask != 3, np.uint8(1))
        bounds = np.logical_and(bounds, da_mask == 1)  # make sure not to overwrite
        n = np.count_nonzero(bounds.values)
        if n > 0:
            da_mask = da_mask.where(~bounds, np.uint8(3))
            self.set_grid(da_mask, "msk")
            self.logger.debug(f"{n:d} outflow (mask=3) boundary cells set.")
        if keep_rivers_geom and gdf_riv is not None:
            gdf_riv = gdf_riv.to_crs(self.crs.to_epsg())
            gdf_riv.index = gdf_riv.index.values + 1  # one based index
            self.set_geoms(gdf_riv, name="rivers_out")

    def setup_cn_infiltration(self, cn_fn="gcn250", antecedent_runoff_conditions="avg"):
        """Setup model potential maximum soil moisture retention map (scsfile)
        from gridded curve number map.

        Adds model layers:

        * **scs** map: potential maximum soil moisture retention [inch]

        Parameters
        ---------
        cn_fn: str, optional
            Name of gridded curve number map.

            * Required layers without antecedent runoff conditions: ['cn']
            * Required layers with antecedent runoff conditions: ['cn_dry', 'cn_avg', 'cn_wet']
        antecedent_runoff_conditions: {'dry', 'avg', 'wet'}, optional
            Antecedent runoff conditions.
            None if data has no antecedent runoff conditions.
            By default `avg`
        """
        # get data
        v = "cn"
        if antecedent_runoff_conditions:
            v = f"cn_{antecedent_runoff_conditions}"
        da_org = self.data_catalog.get_rasterdataset(
            cn_fn, geom=self.region, buffer=10, variables=[v]
        )
        # reproject using median
        da_cn = da_org.raster.reproject_like(self.grid, method="med")
        # CN=100 based on water mask
        if "rivmsk" in self.grid:
            self.logger.info(
                'Updating CN map based on "rivmsk" from setup_river_hydrography method.'
            )
            da_cn = da_cn.where(self.grid["rivmsk"] == 0, 100)
        # convert to potential maximum soil moisture retention S (1000/CN - 10) [inch]
        da_scs = workflows.cn_to_s(da_cn, self.mask > 0).round(3)
        # set grid
        mname = "scs"
        da_scs.attrs.update(**self._ATTRS.get(mname, {}))
        self.set_grid(da_scs, name=mname)
        # update config: remove default infiltration values and set scs map
        self.config.pop("qinf", None)
        self.set_config(f"{mname}file", f"sfincs.{mname}")

    def create_manning_roughness(
        self,
        datasets_rgh: List[dict],
        manning_land=0.04,
        manning_sea=0.02,
        rgh_lev_land=0,
    ):
        """Create model manning roughness map (manningfile) from gridded manning data or a combinataion of gridded
        land-use/land-cover map and manning roughness mapping table.

        Parameters
        ----------
        datsets_rgh : List[dict],
            List of dictionaries with Manning's n data, each containing an xarray.DataSet with manning values and optional merge arguments
        manning_land, manning_sea : float, optional
            Constant manning roughness values for land and sea, by default 0.04 and 0.02 s.m-1/3
            Note that these values are only used when no Manning's n datasets are provided, or to fill the nodata values
        rgh_lev_land : float, optional
            Elevation level to distinguish land and sea roughness (when using manning_land and manning_sea), by default 0.0
        """        
        fromdep = len(datasets_rgh) == 0
        if self.grid_type == "regular":
            if len(datasets_rgh) > 0:
                da_man = workflows.merge_multi_dataarrays(
                    da_list=datasets_rgh,
                    da_like=self.mask,
                    interp_method="linear",
                    logger=logger,
                )
                fromdep = np.isnan(da_man).where(self.mask > 0, False).any()
            if "dep" in self.grid and fromdep:
                da_man0 = xr.where(
                    self.grid["dep"] >= rgh_lev_land, manning_land, manning_sea
                )
            elif fromdep:
                da_man0 = xr.full_like(self.mask, manning_land, dtype=np.float32)

            if len(datasets_rgh) > 0 and fromdep:
                print("WARNING: nan values in manning roughness array")
                da_man = da_man.where(~np.isnan(da_man), da_man0)
            elif fromdep:
                da_man = da_man0
            da_man.raster.set_nodata(-9999.0)

            # set grid
            mname = "manning"
            da_man.attrs.update(**self._ATTRS.get(mname, {}))
            self.set_grid(da_man, name=mname)
            # update config: remove default manning values and set maning map
            for v in ["manning_land", "manning_sea", "rgh_lev_land"]:
                self.config.pop(v, None)
            self.set_config(f"{mname}file", f"sfincs.{mname[:3]}")

    def setup_manning_roughness(
        self,
        datasets_rgh: List[dict] = [],
        manning_land=0.04,
        manning_sea=0.02,
        rgh_lev_land=0,
    ):
        """Setup model manning roughness map (manningfile) from gridded manning data or a combinataion of gridded
        land-use/land-cover map and manning roughness mapping table.

        Adds model layers:

        * **man** map: manning roughness coefficient [s.m-1/3]

        Parameters
        ---------
        datasets_rgh : List[dict], optional
            List of dictionaries with Manning's n datasets. Each dictionary should at least contain one of the following:
            * (1) manning_fn: filename (or Path) of gridded data with manning values
            * (2) lulc_fn (and map_fn) :a combination of a filename of gridded landuse/landcover and a mapping table.
            In additon, optional merge arguments can be provided e.g.: merge_method, gdf_valid_fn 
        manning_land, manning_sea : float, optional
            Constant manning roughness values for land and sea, by default 0.04 and 0.02 s.m-1/3
            Note that these values are only used when no Manning's n datasets are provided, or to fill the nodata values
        rgh_lev_land : float, optional
            Elevation level to distinguish land and sea roughness (when using manning_land and manning_sea), by default 0.0

        See Also
        --------
        :py:meth:'SfincsModel.create_manning_roughness'

        """

        if len(datasets_rgh) > 0:
            datasets_rgh = self._parse_datasets_rgh(datasets_rgh)
        else:
            datasets_rgh = []

        self.create_manning_roughness(
            datasets_rgh=datasets_rgh,
            manning_land=manning_land,
            manning_sea=manning_sea,
            rgh_lev_land=rgh_lev_land,
        )

    def create_observation_points(
        self, gdf_obs: gpd.GeoDataFrame, overwrite: bool = False
    ):
        """Creat model observation point locations.

        Parameters
        ----------
        gdf_obs : gpd.GeoDataFrame
            Geodataframe with observation point locations.
        overwrite : bool, optional
            If True, overwrite existing observation_points instead of appending the new observation_points.
        """
        name = self._GEOMS["observation_points"]

        if not overwrite and name in self.geoms:
            gdf0 = self._geoms.pop(name)
            gdf_obs = gpd.GeoDataFrame(pd.concat([gdf_obs, gdf0], ignore_index=True))
            self.logger.info(f"Adding new observation points to existing ones.")
        self.set_geoms(gdf_obs, name)
        self.set_config(f"{name}file", f"sfincs.{name}")

    def setup_observation_points(
        self, obs_fn: Union[str, Path], overwrite: bool = False, **kwargs
    ):
        """Setup model observation point locations.

        Adds model layers:

        * **obs** geom: observation point locations

        Parameters
        ---------
        obs_fn: str, Path
            Path to observation points geometry file.
            See :py:meth:`hydromt.open_vector`, for accepted files.
        overwrite: bool, optional
            If True, overwrite existing observation points instead of appending the new observation points.
        """
        name = self._GEOMS["observation_points"]

        # ensure the catalog is loaded before adding any new entries
        self.data_catalog.sources

        gdf = self.data_catalog.get_geodataframe(
            obs_fn, geom=self.region, assert_gtype="Point", **kwargs
        ).to_crs(self.crs)

        self.create_observation_points(gdf_obs=gdf, overwrite=overwrite)
        self.logger.info(f"{name} set based on {obs_fn}")

    def create_structures(
        self,
        gdf_structures: gpd.GeoDataFrame,
        stype: str,
        dz: float = None,
        overwrite: bool = False,
    ):
        """Create thin dam or weir structures.

        Adds model layer (depending on `stype`):

        * **thd** geom: thin dam
        * **weir** geom: weir / levee

        Parameters
        ----------
        gdf_structures : gpd.GeoDataFrame
            GeoDataFrame with structure locations and attributes.
        stype : str
            Structure type.
        dz : float, optional
            If provided, for weir structures the z value is calculated from
            the model elevation (dep) plus dz.
        overwrite : bool, optional
            If True, overwrite existing 'stype' structures instead of appending the
            new structures.
        """

        cols = {
            "thd": ["name", "geometry"],
            "weir": ["name", "z", "par1", "geometry"],
        }
        assert stype in cols
        gdf = gdf_structures[
            [c for c in cols[stype] if c in gdf_structures.columns]
        ]  # keep relevant cols

        structs = utils.gdf2linestring(gdf)  # check if it parsed correct
        # sample zb values from dep file and set z = zb + dz
        if stype == "weir" and dz is not None:
            elv = self.grid["dep"]
            structs_out = []
            for s in structs:
                pnts = gpd.points_from_xy(x=s["x"], y=s["y"])
                zb = elv.raster.sample(gpd.GeoDataFrame(geometry=pnts, crs=self.crs))
                s["z"] = zb.values + float(dz)
                structs_out.append(s)
            gdf = utils.linestring2gdf(structs_out, crs=self.crs)
        # Else function if you define elevation of weir
        elif stype == "weir" and np.any(["z" not in s for s in structs]):
            raise ValueError("Weir structure requires z values.")
        # combine with existing structures if present
        if not overwrite and stype in self.geoms:
            gdf0 = self._geoms.pop(stype)
            gdf = gpd.GeoDataFrame(pd.concat([gdf, gdf0], ignore_index=True))
            self.logger.info(f"Adding {stype} structures to existing structures.")

        # set structures
        self.set_geoms(gdf, stype)
        self.set_config(f"{stype}file", f"sfincs.{stype}")

    def setup_structures(
        self,
        structures_fn: Union[str, Path],
        stype: str,
        dz: float = None,
        overwrite: bool = False,
        **kwargs,
    ):
        """Setup thin dam or weir structures.

        Adds model layer (depending on `stype`):

        * **thd** geom: thin dam
        * **weir** geom: weir / levee

        Parameters
        ----------
        structures_fn : str, Path
            Path to structure line geometry file.
            The "name" (for thd and weir), "z" and "par1" (for weir only) are optional.
            For weirs: `dz` must be provided if gdf has no "z" column or Z LineString;
            "par1" defaults to 0.6 if gdf has no "par1" column.
        stype : {'thd', 'weir'}
            Structure type.
        overwrite: bool, optional
            If True, overwrite existing 'stype' structures instead of appending the
            new structures.
        dz: float, optional
            If provided, for weir structures the z value is calculated from
            the model elevation (dep) plus dz.
        """

        # read, clip and reproject
        gdf = self.data_catalog.get_geodataframe(
            structures_fn, geom=self.region, **kwargs
        ).to_crs(self.crs)

        self.create_structures(
            gdf_structures=gdf, stype=stype, dz=dz, overwrite=overwrite
        )
        self.logger.info(f"{stype} structure set based on {structures_fn}")

    ### FORCING
    def set_forcing_1d(
        self,
        df_ts: pd.DataFrame = None,
        gdf_locs: gpd.GeoDataFrame = None,
        name: str = "bzs",
        merge: bool = True,
    ):
        """Set 1D forcing time series.

        Forcing time series with existing column names (i.e. station index) are overwritten.
        Forcing time series with new column names are added to the existing forcing.

        New forcing time series should be accompanied by associated locations.

        In case the forcing time series have a numeric index, the index is converted to
        a datetime index assuming the index is in seconds since tref.

        Parameters
        ----------
        df_ts : pd.DataFrame, optional
            1D forcing time series data. If None, dummy forcing data is added.
        gdf_locs : gpd.GeoDataFrame, optional
            Location of waterlevel boundary points. If None, the currently set locations are used.
        name : str, optional
            Name of the waterlevel boundary time series file, by default 'bzs'.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.
        """
        # check dtypes
        if gdf_locs is not None:
            if not isinstance(gdf_locs, gpd.GeoDataFrame):
                raise ValueError("gdf_locs must be a gpd.GeoDataFrame")
            if not gdf_locs.index.is_integer() and gdf_locs.index.is_unique:
                raise ValueError("gdf_locs index must be unique integer values")
            if gdf_locs.crs != self.crs:
                gdf_locs = gdf_locs.to_crs(self.crs)
        if df_ts is not None:
            if not isinstance(df_ts, pd.DataFrame):
                raise ValueError("df_ts must be a pd.DataFrame")
            if not df_ts.columns.is_integer() and df_ts.columns.is_unique:
                raise ValueError("df_ts column names must be unique integer values")
        # parse datetime index
        if df_ts is not None and df_ts.index.is_numeric():
            if "tref" not in self.config:
                raise ValueError(
                    "tref must be set in config to convert numeric index to datetime index"
                )
            tref = utils.parse_datetime(self.config["tref"])
            df_ts.index = tref + pd.to_timedelta(df_ts.index, unit="sec")
        # merge with existing data
        if name in self.forcing and merge:
            # read existing data
            da = self.forcing[name]
            gdf0 = da.vector.to_gdf()
            df0 = da.transpose(..., da.vector.index_dim).to_pandas()
            if gdf_locs is None:
                gdf_locs = gdf0
            elif set(gdf0.index) != set(gdf_locs.index):
                # merge locations; overwrite existing locations with the same name
                gdf0 = gdf0.drop(gdf_locs.index, errors="ignore")
                gdf_locs = pd.concat([gdf0, gdf_locs], axis=0).sort_index()
                # gdf_locs = gpd.GeoDataFrame(gdf_locs, crs=gdf0.crs)
                df0 = df0.reindex(gdf_locs.index, axis=1, fill_value=0)
            if df_ts is None:
                df_ts = df0
            elif set(df0.columns) != set(df_ts.columns):
                # merge timeseries; overwrite existing timeseries with the same name
                df0 = df0.drop(columns=df_ts.columns, errors="ignore")
                df_ts = pd.concat([df0, df_ts], axis=1).sort_index()
                # use linear interpolation and backfill to fill in missing values
                df_ts = df_ts.sort_index()
                df_ts = df_ts.interpolate(method="linear").bfill().fillna(0)
        # location data is required
        if gdf_locs is None:
            raise ValueError(
                f"gdf_locs must be provided if not merged with existing {name} forcing data"
            )
        # fill in missing timeseries
        if df_ts is None:
            df_ts = pd.DataFrame(
                index=pd.date_range(*self.get_model_time(), periods=2),
                data=0,
                columns=gdf_locs.index,
            )
        # set forcing with consistent names
        if not set(gdf_locs.index) == set(df_ts.columns):
            raise ValueError("The gdf_locs index and df_ts columns must be the same")
        gdf_locs.index.name = "index"
        df_ts.columns.name = "index"
        df_ts.index.name = "time"
        da = GeoDataArray.from_gdf(gdf_locs.to_crs(self.crs), data=df_ts, name=name)
        self.set_forcing(da.transpose("time", "index"))

    def create_waterlevel_forcing(
        self,
        df_ts: pd.DataFrame = None,
        gdf_locs: gpd.GeoDataFrame = None,
        da_offset: RasterDataArray = None,
        merge=True,
    ):
        """Create waterlevel boundary time series.

        The vertical reference of the waterlevel data can be corrected to match
        the vertical reference of the model elevation (dep) layer by adding
        a local offset value derived from the `da_offset` map to the waterlevels,
        e.g. mean dynamic topography for difference between EGM and MSL levels.

        For more options, see `set_forcing_1d`.

        Parameters
        ----------
        df_ts : pd.DataFrame
            Waterlevel time series data.
        gdf_locs : gpd.GeoDataFrame, optional
            Location of waterlevel boundary points.
        da_offset : RasterDataArray, optional
            Raster with vertical offset to apply to the waterlevel time series.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.
        """
        if gdf_locs is None and "bzs" not in self.forcing:
            raise ValueError("No waterlevel boundary (bnd) points set.")
        if da_offset is not None and gdf_locs is not None:
            offset_pnts = da_offset.raster.sample(gdf_locs)
            df_offset = offset_pnts.to_pandas().reindex(df_ts.columns)
            df_ts = df_ts + df_offset.fillna(0)
            offset_avg = offset_pnts.mean().values
            self.logger.debug(
                f"waterlevel forcing: applied offset (avg: {offset_avg:+.2f})"
            )
        self.set_forcing_1d(df_ts, gdf_locs, name="bzs", merge=merge)

    def setup_waterlevel_forcing(
        self,
        geodataset_fn=None,
        timeseries_fn=None,
        offset_fn=None,
        buffer=5e3,
        merge: bool = True,
    ):
        """Create waterlevel boundary time series.

        Use `geodataset_fn` to set the waterlevel boundary from a dataset of point location
        timeseries. The dataset is clipped to the model region plus `buffer` [m], and
        model time based on the model config tstart and tstop entries.

        Use `timeseries_fn` to update the waterlevel boundary from a timeseries file.

        For more details, see `create_waterlevel_forcing` and `set_forcing_1d`.

        Adds model forcing layers:

        * **bzs** forcing: waterlevel time series [m+ref]

        Parameters
        ----------
        geodataset_fn: str, Path, optional
            Path or data source name for geospatial point timeseries file,
        timeseries_fn: str, Path, optional
            Path or data source name for timeseries file
        offset_fn: str, optional
            Path or data source name for gridded offset between vertical reference of elevation and waterlevel data,
            Adds to the waterlevel data before merging.
        buffer: float, optional
            Buffer [m] around model water level boundary cells to select waterlevel gauges,
            by default 5 km.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.

        """
        tstart, tstop = self.get_model_time()  # model time
        kwargs = {}
        if geodataset_fn is not None:
            # buffer around msk==2 values
            if np.any(self.mask == 2):
                region = self.mask.where(self.mask == 2, 0).raster.vectorize()
            else:
                region = self.region
            # read and clip data in time & space
            da = self.data_catalog.get_geodataset(
                geodataset_fn,
                geom=region,
                buffer=buffer,
                variables=["waterlevel"],
                time_tuple=(tstart, tstop),
                crs=self.crs,
            )
            kwargs.update(
                df_ts=da.transpose(..., da.vector.index_dim).to_pandas(),
                gdf_locs=da.vector.to_gdf(),
            )
        elif timeseries_fn is not None:
            df = self.data_catalog.get_dataframe(
                timeseries_fn,
                time_tuple=(tstart, tstop),
                # kwargs below only applied if timeseries_fn not in data catalog
                parse_dates=True,
                index_col=0,
            )
            df.columns = df.columns.map(int)  # parse column names to integers
            kwargs.update(df_ts=df)
        else:
            raise ValueError("Either geodataset_fn or timeseries_fn must be provided")
        if offset_fn is not None:
            da = self.data_catalog.get_rasterdataset(offset_fn)
            kwargs.update(da_offset=da)
        # set/ update forcing
        self.create_waterlevel_forcing(merge=merge, **kwargs)

    def setup_waterlevel_bnd_from_mask(
        self,
        distance: float = 1e4,
        merge: bool = True,
    ):
        """Create waterlevel boundary points along the model waterlevel boundary (msk=2).

        Parameters
        ----------
        distance: float, optional
            Distance [m] between waterlevel boundary points,
            by default 10 km.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.
        """
        # get waterlevel boundary vector based on mask
        gdf_msk = utils.get_bounds_vector(self.mask)
        gdf_msk2 = gdf_msk[gdf_msk["value"] == 2]

        # create points along boundary
        points = []
        for _,row in gdf_msk2.iterrows():
            distances = np.arange(0, row.geometry.length, distance)
            for d in distances:
                point = row.geometry.interpolate(d)
                points.append((point.x, point.y))

        # create geodataframe with points
        gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(*zip(*points)), crs=self.crs)

        # set waterlevel boundary
        self.create_waterlevel_forcing(gdf_locs=gdf, merge=merge)

    def create_discharge_forcing(
        self,
        df_ts: pd.DataFrame = None,
        gdf_locs: gpd.GeoDataFrame = None,
        merge=True,
    ):
        """Create discharge boundary time series.

        For more options, see `set_forcing_1d`.

        Parameters
        ----------
        df_ts : pd.DataFrame
            Waterlevel time series data.
        gdf_locs : gpd.GeoDataFrame, optional
            Location of waterlevel boundary points.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.
        """
        if gdf_locs is None and "dis" not in self.forcing:
            raise ValueError("No discharge inflow (src) points set.")
        self.set_forcing_1d(df_ts, gdf_locs, name="dis", merge=merge)

    def setup_discharge_forcing(
        self, geodataset_fn=None, timeseries_fn=None, merge=True
    ):
        """Setup discharge boundary point locations (src) and time series (dis).

        Use `geodataset_fn` to set the discharge boundary from a dataset of point location
        timeseries. Only locations within the model domain are selected.

        Use `timeseries_fn` to set discharge boundary conditions to pre-set (src) locations,
        e.g. after the :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow` method.

        The dataset/timeseries are clipped to the model time based on the model config
        tstart and tstop entries.

        Adds model layers:

        * **dis** forcing: discharge time series [m3/s]

        Parameters
        ----------
        geodataset_fn: str, Path
            Path or data source name for geospatial point timeseries file.
            This can either be a netcdf file with geospatial coordinates
            or a combined point location file with a timeseries data csv file
            which can be setup through the data_catalog yml file.

            * Required variables if netcdf: ['discharge']
            * Required coordinates if netcdf: ['time', 'index', 'y', 'x']
        timeseries_fn: str, Path
            Path to tabulated timeseries csv file with time index in first column
            and location IDs in the first row,
            see :py:meth:`hydromt.open_timeseries_from_table`, for details.
            NOTE: tabulated timeseries files can only in combination with point location
            coordinates be set as a geodataset in the data_catalog yml file.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.
        """
        tstart, tstop = self.get_model_time()  # time slice
        kwargs = {}
        if geodataset_fn is not None:
            # read and clip data in time & space
            da = self.data_catalog.get_geodataset(
                geodataset_fn,
                geom=self.region,
                variables=["discharge"],
                time_tuple=(tstart, tstop),
                crs=self.crs,
            )
            kwargs.update(
                df_ts=da.transpose(..., da.vector.index_dim).to_pandas(),
                gdf_locs=da.vector.to_gdf(),
            )
        elif timeseries_fn is not None:
            df = self.data_catalog.get_dataframe(
                timeseries_fn,
                time_tuple=(tstart, tstop),
                # kwargs below only applied if timeseries_fn not in data catalog
                parse_dates=True,
                index_col=0,
            )
            df.columns = df.columns.map(int)  # parse column names to integers
            kwargs.update(df_ts=df)
        else:
            raise ValueError("Either geodataset_fn or timeseries_fn must be provided")
        # set/ update forcing
        self.create_discharge_forcing(merge=merge, **kwargs)

    def setup_discharge_forcing_from_grid(
        self,
        discharge_fn,
        locs_fn=None,
        uparea_fn=None,
        wdw=1,
        rel_error=0.05,
        abs_error=50,
    ):
        """Setup discharge boundary location (src) and timeseries (dis) based on a
        gridded discharge dataset.

        If `locs_fn` is not provided, the discharge source locations are expected to be
        pre-set, e.g. using the :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow` method.

        If an upstream area grid is provided the discharge boundary condition is
        snapped to the best fitting grid cell within a `wdw` neighboring cells.
        The best fit is dermined based on the minimal relative upstream area error if
        an upstream area value is available for the discharge boundary locations;
        otherwise it is based on maximum upstream area.

        Adds model layers:

        * **dis** forcing: discharge time series [m3/s]
        * **src** geom: discharge gauge point locations

        Adds meta layer (not used by SFINCS):

        * **src_snapped** geom: snapped gauge location on discharge grid

        Parameters
        ----------
        discharge_fn: str, Path, optional
            Path or data source name for gridded discharge timeseries dataset.

            * Required variables: ['discharge' (m3/s)]
            * Required coordinates: ['time', 'y', 'x']
        locs_fn: str, Path, optional
            Path or data source name for point location dataset. Not required if
            point location have previously been set with :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow`
            See :py:meth:`hydromt.open_vector`, for accepted files.

        uparea_fn: str, Path, optional
            Path to upstream area grid in gdal (e.g. geotiff) or netcdf format.

            * Required variables: ['uparea' (km2)]
        wdw: int, optional
            Window size in number of cells around discharge boundary locations
            to snap to, only used if ``uparea_fn`` is provided. By default 1.
        rel_error, abs_error: float, optional
            Maximum relative error (default 0.05) and absolute error (default 50 km2)
            between the discharge boundary location upstream area and the upstream area of
            the best fit grid cell, only used if "discharge" geoms has a "uparea" column.
        """
        name = "discharge"
        fname = self._FORCING_1D[name][0]
        if locs_fn is not None:
            gdf = self.data_catalog.get_geodataframe(
                locs_fn, geom=self.region, assert_gtype="Point"
            ).to_crs(self.crs)
        elif fname in self.forcing:
            da = self.forcing[fname]
            gdf = da.vector.to_gdf()
        else:
            self.logger.warning(
                'No discharge inflow points in geoms. Provide locations using "locs_fn" or '
                'run "setup_river_inflow()" method first to determine inflow locations.'
            )
            return
        # read data
        ds = self.data_catalog.get_rasterdataset(
            discharge_fn,
            geom=self.region,
            buffer=2,
            time_tuple=self.get_model_time(),  # model time
            variables=[name],
            single_var_as_array=False,
        )
        if uparea_fn is not None and "uparea" in gdf.columns:
            da_upa = self.data_catalog.get_rasterdataset(
                uparea_fn, geom=self.region, buffer=2, variables=["uparea"]
            )
            # make sure ds and da_upa align
            ds["uparea"] = da_upa.raster.reproject_like(ds, method="nearest")
        elif "uparea" not in gdf.columns:
            self.logger.warning('No "uparea" column found in location data.')

        # TODO move to create method?
        ds_snapped = workflows.snap_discharge(
            ds=ds,
            gdf=gdf,
            wdw=wdw,
            rel_error=rel_error,
            abs_error=abs_error,
            uparea_name="uparea",
            discharge_name=name,
            logger=self.logger,
        )
        # set zeros for src points without matching discharge
        da_q = ds_snapped[name].reindex(index=gdf.index, fill_value=0).fillna(0)
        df_q = da_q.transpose("time", ...).to_pandas()
        # update forcing
        self.set_forcing_1d(df_ts=df_q, gdf_locs=gdf, name=fname)
        # keep snapped locations
        self.set_geoms(
            ds_snapped.vector.to_gdf(), f"{self._FORCING_1D[name][1]}_snapped"
        )

    def setup_precip_forcing_from_grid(
        self, precip_fn=None, dst_res=None, aggregate=False, **kwargs
    ):
        """Setup precipitation forcing from a gridded spatially varying data source.

        If aggregate is True, spatially uniform precipitation forcing is added to
        the model based on the mean precipitation over the model domain.
        If aggregate is False, distributed precipitation is added to the model as netcdf file.
        The data is reprojected to the model CRS (and destination resolution `dst_res` if provided).

        Adds one of these model layer:

        * **netamprfile** forcing: distributed precipitation [mm/hr]
        * **precipfile** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        precip_fn, str, Path
            Path to precipitation rasterdataset netcdf file.

            * Required variables: ['precip' (mm)]
            * Required coordinates: ['time', 'y', 'x']

        dst_res: float
            output resolution (m), by default None and computed from source data.
            Only used in combination with aggregate=False
        aggregate: bool, {'mean', 'median'}, optional
            Method to aggregate distributed input precipitation data. If True, mean
            aggregation is used, if False (default) the data is not aggregated and
            spatially distributed precipitation is returned.
        """
        variable = "precip"
        # get data for model domain and config time range
        precip = self.data_catalog.get_rasterdataset(
            precip_fn,
            geom=self.region,
            buffer=2,
            time_tuple=self.get_model_time(),
            variables=[variable],
        )

        # TODO move to create method
        # aggregate or reproject in space
        if aggregate:
            stat = aggregate if isinstance(aggregate, str) else "mean"
            self.logger.debug(f"Aggregate {variable} using {stat}.")
            zone = self.region.dissolve()  # make sure we have a single (multi)polygon
            precip_out = precip.raster.zonal_stats(zone, stats=stat)[f"precip_{stat}"]
            df_ts = precip_out.where(precip_out >= 0, 0).fillna(0).squeeze().to_pandas()
            self.create_precip_forcing(df_ts)
        else:
            # reproject to model utm crs
            # NOTE: currently SFINCS errors (stack overflow) on large files,
            # downscaling to model grid is not recommended
            kwargs0 = dict(align=dst_res is not None, method="nearest_index")
            kwargs0.update(kwargs)
            meth = kwargs0["method"]
            self.logger.debug(f"Resample {variable} using {meth}.")
            precip_out = precip.raster.reproject(
                dst_crs=self.crs, dst_res=dst_res, **kwargs
            ).fillna(0)

            # resample in time
            precip_out = hydromt.workflows.resample_time(
                precip_out,
                freq=pd.to_timedelta("1H"),
                conserve_mass=True,
                upsampling="bfill",
                downsampling="sum",
                logger=self.logger,
            ).rename("precip")

            # add to forcing
            self.set_forcing(precip_out, name="precip")

    def create_precip_forcing(self, df_ts: pd.Series):
        """Create uniform precipitation forcing (precip).

        Adds model layers:

        * **precip** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        df_ts, pandas.DataFrame
            Timeseries dataframe with time index and location IDs as columns.
        """
        if isinstance(df_ts, pd.DataFrame):
            df_ts = df_ts.squeeze()
        if not isinstance(df_ts, pd.Series):
            raise ValueError("df_ts must be a pandas.Series")
        df_ts.name = "precip"
        df_ts.index.name = "time"
        self.set_forcing(df_ts.to_xarray(), name="precip")

    def setup_precip_forcing(self, timeseries_fn):
        """Setup spatially uniform precipitation forcing (precip).

        Adds model layers:

        * **precipfile** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        timeseries_fn, str, Path
            Path to tabulated timeseries csv file with time index in first column
            and location IDs in the first row,
            see :py:meth:`hydromt.open_timeseries_from_table`, for details.
            Note: tabulated timeseries files cannot yet be set through the data_catalog yml file.
        """
        tstart, tstop = self.get_model_time()
        df_ts = self.data_catalog.get_dataframe(
            timeseries_fn,
            time_tuple=(tstart, tstop),
            # kwargs below only applied if timeseries_fn not in data catalog
            parse_dates=True,
            index_col=0,
        )
        self.create_precip_forcing(df_ts)

    def create_index_tiles(
        self,
        path: Union[str, Path] = None,
        region: gpd.GeoDataFrame = None,
        zoom_range: Union[int, List[int]] = [0, 13],
        fmt: str = "bin",
    ):
        """Create index tiles for a region. Index tiles are used to quickly map webmercator tiles to the correct SFINCS cell.

        Parameters
        ----------
        path : Union[str, Path]
            Directory in which to store the index tiles, if None, the model root + tiles is used.
            Note that the index tiles are stored in a subdirectory named index.
        region : gpd.GeoDataFrame
            GeoDataFrame defining the area of interest, if None, the model region is used.
        zoom_range : Union[int, List[int]], optional
            Range of zoom levels for which tiles are created, by default [0,13]
        fmt : str, optional
            Format of the index tiles, either "bin" (binary, default) or "png".
        """
        if path is None:
            path = os.path.join(self.root, "tiles")
        if region is None:
            region = self.region

        if self.grid_type == "regular":
            self.reggrid.create_index_tiles(
                region=region, root=path, zoom_range=zoom_range, fmt=fmt
            )
        elif self.grid_type == "quadtree":
            raise NotImplementedError(
                "Index tiles not yet implemented for quadtree grids."
            )

    def create_topobathy_tiles(
        self,
        path: Union[str, Path] = None,
        region: gpd.GeoDataFrame = None,
        datasets_dep: List[dict] = [],
        index_path: Union[str, Path] = None,
        zoom_range: Union[int, List[int]] = [0, 13],
        z_range: List[int] = [-20000.0, 20000.0],
        fmt: str = "bin",
    ):
        """Create webmercator tiles for merged topography and bathymetry.

        Parameters
        ----------
        path : Union[str, Path]
            Directory in which to store the index tiles, if None, the model root + tiles is used.
            Note that the tiles are stored in a subdirectory named topobathy.
        region : gpd.GeoDataFrame
            GeoDataFrame defining the area of interest, if None, the model region is used.
        datasets_dep : List[dict]
            List of dict containing xarray.DataArray and metadata for each dataset.
        index_path : Union[str, Path], optional
            Directory where index tiles are stored. If defined, topobathy tiles are only created where index tiles are present.
        zoom_range : Union[int, List[int]], optional
            Range of zoom levels for which tiles are created, by default [0,13]
        z_range : List[int], optional
            Range of valid elevations that are included in the topobathy tiles, by default [-20000.0, 20000.0]
        fmt : str, optional
            Format of the topobathy tiles: "bin" (binary, default), "png" or "tif".
        """

        if path is None:
            path = os.path.join(self.root, "tiles")
        if region is None:
            # if region not provided, use model region
            region = self.region

        # if no datasets provided, check if high-res subgrid geotiff is there
        if len(datasets_dep) == 0:
            if os.path.exists(os.path.join(self.root, "tiles", "subgrid")):
                # check if there is a dep_subgrid.tif
                dep = os.path.join(self.root, "tiles", "subgrid", "dep_subgrid.tif")
                if os.path.exists(dep):
                    da = self.data_catalog.get_rasterdataset(dep)
                    datasets_dep.append({"da": da})
                else:
                    raise ValueError("No topobathy datasets provided.")

        # create topobathy tiles
        workflows.tiling.create_topobathy_tiles(
            root=path,
            region=region,
            datasets_dep=datasets_dep,
            index_path=index_path,
            zoom_range=zoom_range,
            z_range=z_range,
            fmt=fmt,
        )

    def setup_tiles(
        self,
        path: Union[str, Path] = None,
        region: dict = None,
        datasets_dep: List[dict] = [],
        zoom_range: Union[int, List[int]] = [0, 13],
        z_range: List[int] = [-20000.0, 20000.0],
        fmt: str = "bin",
    ):
        """Create both index and topobathy tiles in webmercator format.

        Parameters
        ----------
        path : Union[str, Path]
            Directory in which to store the index tiles, if None, the model root + tiles is used.
        region : dict
            Dictionary describing region of interest, e.g.:
            * {'bbox': [xmin, ymin, xmax, ymax]}. Note bbox should be provided in WGS 84
            * {'geom': 'path/to/polygon_geometry'}
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (dep_fn) and optional merge arguments e.g.:
            [{'dep_fn': merit_hydro, 'zmin': 0.01}, {'dep_fn': gebco, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        zoom_range : Union[int, List[int]], optional
            Range of zoom levels for which tiles are created, by default [0,13]
        z_range : List[int], optional
            Range of valid elevations that are included in the topobathy tiles, by default [-20000.0, 20000.0]
        fmt : str, optional
            Format of the tiles: "bin" (binary, default), "png".
        """
        # use model root if path not provided        
        if path is None:
            path = os.path.join(self.root, "tiles")

        # use model region if region not provided
        if region is None:
            region = self.region
        else:
            _kind, _region = hydromt.workflows.parse_region(region=region)
            if "bbox" in _region:
                bbox = _region["bbox"]
                region = gpd.GeoDataFrame(geometry=[box(*bbox)], crs=4326)
            elif "geom" in _region:
                region = _region["geom"]
                if region.crs is None:
                    raise ValueError('Model region "geom" has no CRS')

        # if only one zoom level is specified, create tiles up to that zoom level (inclusive)
        if isinstance(zoom_range, int):
            zoom_range = [0, zoom_range]

        # create index tiles
        # only binary and png are supported for index tiles so set to binary if tif
        fmt_ind = "bin" if fmt == "tif" else fmt

        self.create_index_tiles(
            path=path, region=region, zoom_range=zoom_range, fmt=fmt_ind
        )

        # compute resolution of highest zoom level
        # resolution of zoom level 0  on equator: 156543.03392804097
        res = 156543.03392804097 / 2 ** zoom_range[1]
        datasets_dep = self._parse_datasets_dep(datasets_dep, res=res)

        # create topobathy tiles
        self.create_topobathy_tiles(
            path=path,
            region=region,
            datasets_dep=datasets_dep,
            index_path = os.path.join(path, "index"),
            zoom_range=zoom_range,
            z_range=z_range,
            fmt=fmt,
        )

    # Plotting
    def plot_forcing(self, fn_out="forcing.png", **kwargs):
        """Plot model timeseries forcing.

        For distributed forcing a spatial avarage is plotted.

        Parameters
        ----------
        fn_out: str
            Path to output figure file.
            If a basename is given it is saved to <model_root>/figs/<fn_out>
            If None, no file is saved.
        forcing : Dict of xr.DataArray
            Model forcing

        Returns
        -------
        fig, axes
            Model fig and ax objects
        """
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        if self.forcing:
            forcing = {}
            for name in self.forcing:
                if isinstance(self.forcing[name], xr.Dataset):
                    continue  # plot only dataarrays
                forcing[name] = self.forcing[name]
                # update missing attributes for plot labels
                forcing[name].attrs.update(**self._ATTRS.get(name, {}))
            if len(forcing) > 0:
                fig, axes = plots.plot_forcing(forcing, **kwargs)
                # set xlim to model tstart - tend
                tstart, tstop = self.get_model_time()
                axes[-1].set_xlim(mdates.date2num([tstart, tstop]))

                # save figure
                if fn_out is not None:
                    if not os.path.isabs(fn_out):
                        fn_out = join(self.root, "figs", fn_out)
                    if not os.path.isdir(dirname(fn_out)):
                        os.makedirs(dirname(fn_out))
                    plt.savefig(fn_out, dpi=225, bbox_inches="tight")
                return fig, axes

    def plot_basemap(
        self,
        fn_out: str = None,
        variable: str = "dep",
        shaded: bool = False,
        plot_bounds: bool = True,
        plot_region: bool = False,
        plot_geoms: bool = True,
        bmap: str = None,
        zoomlevel: int = 11,
        figsize: Tuple[int] = None,
        geom_names: List[str] = None,
        geom_kwargs: Dict = {},
        legend_kwargs: Dict = {},
        **kwargs,
    ):
        """Create basemap plot.

        Parameters
        ----------
        fn_out: str, optional
            Path to output figure file, by default None.
            If a basename is given it is saved to <model_root>/figs/<fn_out>
            If None, no file is saved.
        variable : str, optional
            Map of variable in ds to plot, by default 'dep'
        shaded : bool, optional
            Add shade to variable (only for variable = 'dep' and non-rotated grids),
            by default False
        plot_bounds : bool, optional
            Add waterlevel (msk=2) and open (msk=3) boundary conditions to plot.
        plot_region : bool, optional
            If True, plot region outline.
        plot_geoms : bool, optional
            If True, plot available geoms.
        bmap : {'sat', 'osm'}, optional
            background map, by default None
        zoomlevel : int, optional
            zoomlevel, by default 11
        figsize : Tuple[int], optional
            figure size, by default None
        geom_names : List[str], optional
            list of model geometries to plot, by default all model geometries.
        geom_kwargs : Dict of Dict, optional
            Model geometry styling per geometry, passed to geopandas.GeoDataFrame.plot method.
            For instance: {'src': {'markersize': 30}}.
        legend_kwargs : Dict, optional
            Legend kwargs, passed to ax.legend method.

        Returns
        -------
        fig, axes
            Model fig and ax objects
        """
        import matplotlib.pyplot as plt

        # combine geoms and forcing locations
        sg = self.geoms.copy()
        for fname, gname in self._FORCING_1D.items():
            if fname in self.forcing and gname is not None:
                try:
                    sg.update({gname: self._forcing[fname].vector.to_gdf()})
                except ValueError:
                    self.logger.debug(f'unable to plot forcing location: "{fname}"')

        # make sure grid are set
        if "msk" not in self.grid:
            self.set_grid(self.mask, "msk")

        fig, ax = plots.plot_basemap(
            self.grid,
            sg,
            variable=variable,
            shaded=shaded,
            plot_bounds=plot_bounds,
            plot_region=plot_region,
            plot_geoms=plot_geoms,
            bmap=bmap,
            zoomlevel=zoomlevel,
            figsize=figsize,
            geom_names=geom_names,
            geom_kwargs=geom_kwargs,
            legend_kwargs=legend_kwargs,
            **kwargs,
        )

        if fn_out is not None:
            if not os.path.isabs(fn_out):
                fn_out = join(self.root, "figs", fn_out)
            if not os.path.isdir(dirname(fn_out)):
                os.makedirs(dirname(fn_out))
            plt.savefig(fn_out, dpi=225, bbox_inches="tight")

        return fig, ax

    # I/O
    def read(self, epsg: int = None):
        """Read the complete model schematization and configuration from file."""
        self.read_config(epsg=epsg)
        if epsg is None and "epsg" not in self.config:
            raise ValueError(f"Please specify epsg to read this model")
        self.read_grid()
        self.read_subgrid()
        self.read_geoms()
        self.read_forcing()
        self.logger.info("Model read")

    def write(self):
        """Write the complete model schematization and configuration to file."""
        self.logger.info(f"Writing model data to {self.root}")
        # TODO - add check for subgrid & quadtree > give flags to self.write_grid() and self.write_config()
        self.write_grid()
        self.write_subgrid()
        self.write_geoms()
        self.write_forcing()
        self.write_states()
        # config last; might be udpated when writing maps, states or forcing
        self.write_config()
        # write data catalog with used data sources
        self.write_data_catalog()  # new in hydromt v0.4.4

    def read_grid(self, data_vars: Union[List, str] = None) -> None:
        """Read SFINCS binary grid files and save to `grid` attribute.
        Filenames are taken from the `config` attribute (i.e. input file).
        
        Parameters
        ----------
        data_vars : Union[List, str], optional
            List of data variables to read, by default None (all)
        """
        
        da_lst = []
        if data_vars is None:
            data_vars = self._MAPS
        elif isinstance(data_vars, str):
            data_vars = list(data_vars)

        # read index file
        ind_fn = self.get_config("indexfile", fallback="sfincs.ind", abs_path=True)
        if not isfile(ind_fn):
            raise IOError(f".ind path {ind_fn} does not exist")

        dtypes = {"msk": "u1"}
        mvs = {"msk": 0}
        if self.reggrid is not None:
            ind = self.reggrid.read_ind(ind_fn=ind_fn)

            for name in data_vars:
                if f"{name}file" in self.config:
                    fn = self.get_config(
                        f"{name}file", fallback=f"sfincs.{name}", abs_path=True
                    )
                    if not isfile(fn):
                        self.logger.warning(f"{name}file not found at {fn}")
                        continue
                    dtype = dtypes.get(name, "f4")
                    mv = mvs.get(name, -9999.0)
                    da = self.reggrid.read_map(fn, ind, dtype, mv, name=name)
                    da_lst.append(da)
            ds = xr.merge(da_lst)
            epsg = self.config.get("epsg", None)
            if epsg is not None:
                ds.raster.set_crs(epsg)
            self.set_grid(ds)

            # keep some metadata maps from gis directory
            keep_maps = ["flwdir", "uparea", "rivmsk"]
            fns = glob.glob(join(self.root, "gis", "*.tif"))
            fns = [fn for fn in fns if basename(fn).split(".")[0] in keep_maps]
            if fns:
                ds = hydromt.open_mfraster(fns).load()
                self.set_grid(ds)
                ds.close()

    def write_grid(self, data_vars: Union[List, str] = None):
        """Write SFINCS grid to binary files including map index file.
        Filenames are taken from the `config` attribute (i.e. input file).

        If `write_gis` property is True, all grid variables are written to geotiff
        files in a "gis" subfolder.

        Parameters
        ----------
        data_vars : Union[List, str], optional
            List of data variables to write, by default None (all)
        """
        self._assert_write_mode

        dtypes = {"msk": "u1"}  # default to f4
        if self.reggrid and len(self._grid.data_vars) > 0 and "msk" in self.grid:
            # make sure orientation is S->N
            ds_out = self.grid
            if ds_out.raster.res[1] < 0:
                ds_out = ds_out.raster.flipud()
            mask = ds_out["msk"].values

            self.logger.debug("Write binary map indices based on mask.")
            ind_fn = self.get_config("indexfile", abs_path=True)
            self.reggrid.write_ind(ind_fn=ind_fn, mask=mask)

            if data_vars is None:  # write all maps
                data_vars = [v for v in self._MAPS if v in ds_out]
            elif isinstance(data_vars, str):
                data_vars = list(data_vars)
            self.logger.debug(f"Write binary map files: {data_vars}.")
            for name in data_vars:
                if f"{name}file" not in self.config:
                    self.set_config(f"{name}file", f"sfincs.{name}")
                # do not write depfile if subgrid is used
                if (name == "dep" or name == "manning") and self.subgrid:
                    continue
                self.reggrid.write_map(
                    map_fn=self.get_config(f"{name}file", abs_path=True),
                    data=ds_out[name].values,
                    mask=mask,
                    dtype=dtypes.get(name, "f4"),
                )

        if self._write_gis:
            self.write_raster("grid")

    def read_subgrid(self):
        """Read SFINCS subgrid file and add to `subgrid` attribute.
        Filename is taken from the `config` attribute (i.e. input file)."""
        
        self._assert_read_mode

        if "sbgfile" in self.config:
            fn = self.get_config("sbgfile", abs_path=True)
            if not isfile(fn):
                self.logger.warning(f"sbgfile not found at {fn}")
                return

            self.reggrid.subgrid.load(file_name=fn, mask=self.mask)
            self.subgrid = self.reggrid.subgrid.to_xarray(
                dims=self.mask.raster.dims, coords=self.mask.raster.coords
            )

    def write_subgrid(self):
        """Write SFINCS subgrid file."""
        self._assert_write_mode

        if self.subgrid:
            if f"sbgfile" not in self.config:
                self.set_config(f"sbgfile", f"sfincs.sbg")
            fn = self.get_config(f"sbgfile", abs_path=True)
            self.reggrid.subgrid.save(file_name=fn, mask=self.mask)

    def read_geoms(self):
        """Read geometry files and save to `geoms` attribute.
        Known geometry files mentioned in the sfincs.inp configuration file are read,
        including: bnd/src/obs xy files and thd/weir structure files.

        If other geojson files are present in a "gis" subfolder folder, those are read as well.
        """
        self._assert_read_mode
        # read _GEOMS model files
        for gname in self._GEOMS.values():
            if f"{gname}file" in self.config:
                fn = self.get_config(f"{gname}file", abs_path=True)
                if fn is None:
                    continue
                elif not isfile(fn):
                    self.logger.warning(f"{gname}file not found at {fn}")
                    continue
                if gname in ["thd", "weir"]:
                    struct = utils.read_geoms(fn)
                    gdf = utils.linestring2gdf(struct, crs=self.crs)
                elif gname == "obs":
                    gdf = utils.read_xyn(fn, crs=self.crs)
                else:
                    gdf = utils.read_xy(fn, crs=self.crs)
                self.set_geoms(gdf, name=gname)
        # read additional geojson files from gis directory
        for fn in glob.glob(join(self.root, "gis", "*.geojson")):
            name = basename(fn).replace(".geojson", "")
            gnames = [f[1] for f in self._FORCING_1D.values() if f[1] is not None]
            skip = gnames + list(self._GEOMS.values())
            if name in skip:
                continue
            gdf = hydromt.open_vector(fn, crs=self.crs)
            self.set_geoms(gdf, name=name)

    def write_geoms(self, data_vars: Union[List, str] = None):
        """Write geoms to bnd/src/obs xy files and thd/weir structure files.
        Filenames are based on the `config` attribute.

        If `write_gis` property is True, all geoms are written to geojson
        files in a "gis" subfolder.

        Parameters
        ----------
        data_vars : list of str, optional
            List of data variables to write, by default None (all)

        """
        self._assert_write_mode

        if self.geoms:
            dvars = self._GEOMS.values()
            if data_vars is not None:
                dvars = [name for name in data_vars if name in self._GEOMS.values()]
            self.logger.info("Write geom files")
            for gname, gdf in self.geoms.items():
                if gname in dvars:
                    if f"{gname}file" not in self.config:
                        self.set_config(f"{gname}file", f"sfincs.{gname}")
                    fn = self.get_config(f"{gname}file", abs_path=True)
                    if gname in ["thd", "weir"]:
                        struct = utils.gdf2linestring(gdf)
                        utils.write_geoms(fn, struct, stype=gname)
                    elif gname == "obs":
                        utils.write_xyn(fn, gdf, crs=self.crs)
                    else:
                        utils.write_xy(fn, gdf, fmt="%8.2f")

            # NOTE: all geoms are written to geojson files in a "gis" subfolder
            if self._write_gis:
                self.write_vector(variables=["geoms"])

    def read_forcing(self, data_vars: List = None):
        """Read forcing files and save to `forcing` attribute.
        Known forcing files mentioned in the sfincs.inp configuration file are read,
        including: bzs/dis/precip ascii files and the netampr netcdf file.

        Parameters
        ----------
        data_vars : list of str, optional
            List of data variables to read, by default None (all)
        """
        self._assert_read_mode
        if isinstance(data_vars, str):
            data_vars = list(data_vars)

        # 1D
        dvars_1d = self._FORCING_1D
        if data_vars is not None:
            dvars_1d = [name for name in data_vars if name in dvars_1d]
        tref = utils.parse_datetime(self.config["tref"])
        for name in dvars_1d:
            ts_names, xy_name = self._FORCING_1D[name]
            # read time series
            da_lst = []
            for ts_name in ts_names:
                ts_fn = self.get_config(f"{ts_name}file", abs_path=True)
                if ts_fn is None or not isfile(ts_fn):
                    if ts_fn is not None:
                        self.logger.warning(f"{ts_name}file not found at {ts_fn}")
                    continue
                df = utils.read_timeseries(ts_fn, tref)
                df.index.name = "time"
                if xy_name is not None:
                    df.columns.name = "index"
                    da = xr.DataArray(df, dims=("time", "index"), name=ts_name)
                else:  # spatially uniform forcing
                    da = xr.DataArray(df[df.columns[0]], dims=("time"), name=ts_name)
                da_lst.append(da)
            ds = xr.merge(da_lst[:])
            # read xy
            if xy_name is not None:
                xy_fn = self.get_config(f"{xy_name}file", abs_path=True)
                if xy_fn is None or not isfile(xy_fn):
                    if xy_fn is not None:
                        self.logger.warning(f"{xy_name}file not found at {xy_fn}")
                else:
                    gdf = utils.read_xy(xy_fn, crs=self.crs)
                    # read attribute data from gis files
                    gis_fn = join(self.root, "gis", f"{xy_name}.geojson")
                    if isfile(gis_fn):
                        gdf1 = gpd.read_file(gis_fn)
                        if "index" in gdf1.columns:
                            gdf1 = gdf1.set_index("index")
                            gdf.index = gdf1.index.values
                            ds = ds.assign_coords(index=gdf1.index.values)
                        if np.any(gdf1.columns != "geometry"):
                            gdf = gpd.sjoin(gdf, gdf1, how="left")[gdf1.columns]
                    # set locations as coordinates dataset
                    ds = GeoDataset.from_gdf(gdf, ds, index_dim="index")
            # save in self.forcing
            if len(ds) > 1:
                # keep wave forcing together
                self.set_forcing(ds, name=name, split_dataset=False)
            elif len(ds) > 0:
                self.set_forcing(ds, split_dataset=True)

        # 2D NETCDF format
        dvars_2d = self._FORCING_NET
        if data_vars is not None:
            dvars_2d = [name for name in data_vars if name in dvars_2d]
        for name in dvars_2d:
            fname, rename = self._FORCING_NET[name]
            fn = self.get_config(f"{fname}file", abs_path=True)
            if fn is None or not isfile(fn):
                if fn is not None:
                    self.logger.warning(f"{name}file not found at {fn}")
                continue
            elif name in ["netbndbzsbzi", "netsrcdis"]:
                ds = GeoDataset.from_netcdf(fn, crs=self.crs, chunks="auto")
            else:
                ds = xr.open_dataset(fn, chunks="auto")
            rename = {k: v for k, v in rename.items() if k in ds}
            if len(rename) > 0:
                ds = ds.rename(rename).squeeze(drop=True)[list(rename.values())]
                self.set_forcing(ds, split_dataset=True)
            else:
                logger.warning(f"No forcing variables found in {fname}file")

    def write_forcing(self, data_vars: Union[List, str] = None):
        """Write forcing to ascii or netcdf (netampr) files.
        Filenames are based on the `config` attribute.

        Parameters
        ----------
        data_vars : list of str, optional
            List of data variables to write, by default None (all)
        """
        self._assert_write_mode

        if self.forcing:
            self.logger.info("Write forcing files")

            tref = utils.parse_datetime(self.config["tref"])
            # for nc files -> time in minutes since tref
            tref_str = tref.strftime("%Y-%m-%d %H:%M:%S")

            # 1D timeseries + location text files
            dvars_1d = self._FORCING_1D
            if data_vars is not None:
                dvars_1d = [name for name in data_vars if name in self._FORCING_1D]
            for name in dvars_1d:
                ts_names, xy_name = self._FORCING_1D[name]
                if (
                    name in self._FORCING_NET
                    and f"{self._FORCING_NET[name][0]}file" in self.config
                ):
                    continue  # write NC file instead of text files
                # work with wavespectra dataset and bzs/dis dataarray
                if name in self.forcing and isinstance(self.forcing[name], xr.Dataset):
                    ds = self.forcing[name]
                else:
                    ds = self.forcing  # dict
                # write timeseries
                da = None
                for ts_name in ts_names:
                    if ts_name not in ds or ds[ts_name].ndim > 2:
                        continue
                    # parse data to dataframe
                    da = ds[ts_name].transpose("time", ...)
                    df = da.to_pandas()
                    # get filenames from config
                    if f"{ts_name}file" not in self.config:
                        self.set_config(f"{ts_name}file", f"sfincs.{ts_name}")
                    fn = self.get_config(f"{ts_name}file", abs_path=True)
                    # write timeseries
                    utils.write_timeseries(fn, df, tref)
                # write xy
                if xy_name and da is not None:
                    # parse data to geodataframe
                    try:
                        gdf = da.vector.to_gdf()
                    except Exception:
                        raise ValueError(f"Locations missing for {name} forcing")
                    # get filenames from config
                    if f"{xy_name}file" not in self.config:
                        self.set_config(f"{xy_name}file", f"sfincs.{xy_name}")
                    fn_xy = self.get_config(f"{xy_name}file", abs_path=True)
                    # write xy
                    utils.write_xy(fn_xy, gdf, fmt="%8.2f")
                    # write geojson file to gis folder
                    self.write_vector(variables=f"forcing.{ts_names[0]}")

            # netcdf forcing
            encoding = dict(
                time={"units": f"minutes since {tref_str}", "dtype": "float64"}
            )
            dvars_2d = self._FORCING_NET
            if data_vars is not None:
                dvars_2d = [name for name in data_vars if name in self._FORCING_NET]
            for name in dvars_2d:
                if (
                    name in self._FORCING_1D
                    and f"{self._FORCING_1D[name][1]}file" in self.config
                ):
                    continue  # timeseries + xy file already written
                fname, rename = self._FORCING_NET[name]
                # combine variables and rename to output names
                rename = {v: k for k, v in rename.items() if v in ds}
                if len(rename) == 0:
                    continue
                ds = xr.merge([self.forcing[v] for v in rename.keys()]).rename(rename)
                # get filename from config
                if f"{fname}file" not in self.config:
                    self.set_config(f"{fname}file", f"{name}.nc")
                fn = self.get_config(f"{fname}file", abs_path=True)
                # write 1D timeseries
                if fname in ["netbndbzsbzi", "netsrcdis"]:
                    ds.vector.to_xy().to_netcdf(fn, encoding=encoding)
                    # write geojson file to gis folder
                    self.write_vector(variables=f"forcing.{list(rename.keys())[0]}")
                # write 2D gridded timeseries
                else:
                    ds.to_netcdf(fn, encoding=encoding)

    def read_states(self, crs=None):
        """Read waterlevel state (zsini) from ascii file and save to `states` attribute.
        The inifile if mentioned in the sfincs.inp configuration file is read.

        Parameters
        ----------
        crs: int, CRS
            Coordinate reference system, if provided use instead of epsg code from sfincs.inp
        """
        self._assert_read_mode
        if "inifile" in self.config:
            fn = self.get_config("inifile", abs_path=True)
            if not isfile(fn):
                self.logger.warning("inifile not found at {fn}")
                return
            shape, transform, crs = self.get_spatial_attrs(crs=crs)
            zsini = RasterDataArray.from_numpy(
                data=utils.read_ascii_map(fn),  # orientation S-N
                transform=transform,
                crs=crs,
                nodata=-9999,  # TODO: check what a good nodatavalue is
            )
            if zsini.shape != shape:
                raise ValueError('The shape of "inifile" and maps does not match.')
            if "msk" in self._grid:
                zsini = zsini.where(self.mask != 0, -9999)
            self.set_states(zsini, "zsini")

    def write_states(self, fmt="%8.3f"):
        """Write waterlevel state (zsini)  to ascii map file.
        The filenames is based on the `config` attribute.
        """
        self._assert_write_mode

        assert len(self._states) <= 1
        for name in self._states:
            if f"inifile" not in self.config:
                self.set_config(f"inifile", f"sfincs.{name}")
            fn = self.get_config("inifile", abs_path=True)
            da = self._states[name].fillna(0)  # TODO check proper nodata value
            if da.raster.res[1] < 0:  # orientation is S->N
                da = da.raster.flipud()
            utils.write_ascii_map(fn, da.values, fmt=fmt)
        if self._write_gis:
            self.write_raster("states")

    def read_results(
        self,
        chunksize=100,
        drop=["crs", "sfincsgrid"],
        fn_map="sfincs_map.nc",
        fn_his="sfincs_his.nc",
        **kwargs,
    ):
        """Read results from sfincs_map.nc and sfincs_his.nc and save to the `results` attribute.
        The staggered nc file format is translated into hydromt.RasterDataArray formats.
        Additionally, hmax is computed from zsmax and zb if present.

        Parameters
        ----------
        chunksize: int, optional
            chunk size along time dimension, by default 100
        drop: list, optional
            list of variables to drop, by default ["crs", "sfincsgrid"]
        fn_map: str, optional
            filename of sfincs_map.nc, by default "sfincs_map.nc"
        fn_his: str, optional
            filename of sfincs_his.nc, by default "sfincs_his.nc"
        """
        if not isabs(fn_map):
            fn_map = join(self.root, fn_map)
        if isfile(fn_map):
            ds_face, ds_edge = utils.read_sfincs_map_results(
                fn_map,
                crs=self.crs,
                chunksize=chunksize,
                drop=drop,
                logger=self.logger,
                **kwargs,
            )
            # save as dict of DataArray
            self.set_results(ds_face, split_dataset=True)
            self.set_results(ds_edge, split_dataset=True)

        if not isabs(fn_his):
            fn_his = join(self.root, fn_his)
        if isfile(fn_his):
            ds_his = utils.read_sfincs_his_results(
                fn_his, crs=self.crs, chunksize=chunksize
            )
            # drop double vars (map files has priority)
            drop_vars = [v for v in ds_his.data_vars if v in self._results or v in drop]
            ds_his = ds_his.drop_vars(drop_vars)
            self.set_results(ds_his, split_dataset=True)

    def write_raster(
        self,
        variables=["grid", "states", "results.hmax"],
        root=None,
        driver="GTiff",
        compress="deflate",
        **kwargs,
    ):
        """Write model 2D raster variables to geotiff files.

        NOTE: these files are not used by the model by just saved for visualization/
        analysis purposes.

        Parameters
        ----------
        variables: str, list, optional
            Model variables are a combination of attribute and layer (optional) using <attribute>.<layer> syntax.
            Known ratster attributes are ["grid", "states", "results"].
            Different variables can be combined in a list.
            By default, variables is ["grid", "states", "results.hmax"]
        root: Path, str, optional
            The output folder path. If None it defaults to the <model_root>/gis folder (Default)
        kwargs:
            Key-word arguments passed to hydromt.RasterDataset.to_raster(driver='GTiff', compress='lzw').
        """

        # check variables
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, list):
            raise ValueError(f'"variables" should be a list, not {type(list)}.')
        # check root
        if root is None:
            root = join(self.root, "gis")
        if not os.path.isdir(root):
            os.makedirs(root)
        # save to file
        for var in variables:
            vsplit = var.split(".")
            attr = vsplit[0]
            obj = getattr(self, f"_{attr}")
            if obj is None or len(obj) == 0:
                continue  # empty
            self.logger.info(f"Write raster file(s) for {var} to 'gis' subfolder")
            layers = vsplit[1:] if len(vsplit) >= 2 else list(obj.keys())
            for layer in layers:
                if layer not in obj:
                    self.logger.warning(f"Variable {attr}.{layer} not found: skipping.")
                    continue
                da = obj[layer]
                if len(da.dims) != 2 or "time" in da.dims:
                    continue
                # only write active cells to gis files
                da = da.raster.clip_geom(self.region, mask=True).raster.mask_nodata()
                if da.raster.res[1] > 0:  # make sure orientation is N->S
                    da = da.raster.flipud()
                da.raster.to_raster(
                    join(root, f"{layer}.tif"),
                    driver=driver,
                    compress=compress,
                    **kwargs,
                )

    def write_vector(
        self,
        variables=["geoms", "forcing.bzs", "forcing.dis"],
        root=None,
        gdf=None,
        **kwargs,
    ):
        """Write model vector (geoms) variables to geojson files.

        NOTE: these files are not used by the model by just saved for visualization/
        analysis purposes.

        Parameters
        ----------
        variables: str, list, optional
            geoms variables. By default all geoms are saved.
        root: Path, str, optional
            The output folder path. If None it defaults to the <model_root>/gis folder (Default)
        kwargs:
            Key-word arguments passed to geopandas.GeoDataFrame.to_file(driver='GeoJSON').
        """
        kwargs.update(driver="GeoJSON")  # fixed
        # check variables
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, list):
            raise ValueError(f'"variables" should be a list, not {type(list)}.')
        # check root
        if root is None:
            root = join(self.root, "gis")
        if not os.path.isdir(root):
            os.makedirs(root)
        # save to file
        for var in variables:
            vsplit = var.split(".")
            attr = vsplit[0]
            obj = getattr(self, f"_{attr}")
            if obj is None or len(obj) == 0:
                continue  # empty
            self.logger.info(f"Write vector file(s) for {var} to 'gis' subfolder")
            names = vsplit[1:] if len(vsplit) >= 2 else list(obj.keys())
            for name in names:
                if name not in obj:
                    self.logger.warning(f"Variable {attr}.{name} not found: skipping.")
                    continue
                if isinstance(obj[name], gpd.GeoDataFrame):
                    gdf = obj[name]
                else:
                    try:
                        gdf = obj[name].vector.to_gdf()
                        # xy name -> difficult!
                        name = [
                            v[-1] for v in self._FORCING_1D.values() if name in v[0]
                        ][0]
                    except:
                        self.logger.debug(
                            f"Variable {attr}.{name} could not be written to vector file."
                        )
                        pass
                gdf.to_file(join(root, f"{name}.geojson"), **kwargs)

    ## model configuration

    def read_config(self, config_fn: str = "sfincs.inp", epsg: int = None) -> None:
        """Parse config from SFINCS input file.
        If in write-only mode the config is initialized with default settings.

        Parameters
        ----------
        config_fn: str
            Filename of config file, by default "sfincs.inp".
            If in a different folder than the model root, the root is updated.
        epsg: int
            EPSG code of the model CRS. Only used if missing in the SFINCS input file, by default None.
        """
        inp = SfincsInput()  # initialize with defaults
        if self._read:  # in read-only or append mode, try reading config_fn
            if not isfile(config_fn) and not isabs(config_fn) and self._root:
                # path relative to self.root
                config_fn = abspath(join(self.root, config_fn))
            elif isfile(config_fn) and abspath(dirname(config_fn)) != self._root:
                # new root
                mode = (
                    "r+"
                    if self._write and self._read
                    else ("w" if self._write else "r")
                )
                root = abspath(dirname(config_fn))
                self.logger.warning(f"updating the model root to: {root}")
                self.set_root(root=root, mode=mode)
            else:
                raise IOError(f"SFINCS input file not found {config_fn}")
            # read config_fn
            inp.read(inp_fn=config_fn)
        # overwrite / initialize config attribute
        self._config = inp.to_dict()
        if epsg is not None and "epsg" not in self.config:
            self.config.update(epsg=epsg)
        self.update_grid_from_config()  # update grid properties based on sfincs.inp

    def write_config(self, config_fn: str = "sfincs.inp"):
        """Write config to <root/config_fn>"""
        self._assert_write_mode
        if not isabs(config_fn) and self._root:
            config_fn = join(self.root, config_fn)

        inp = SfincsInput.from_dict(self.config)
        inp.write(inp_fn=abspath(config_fn))

    def update_spatial_attrs(self):
        """Update geospatial `config` (sfincs.inp) attributes based on grid"""
        dx, dy = self.res
        # TODO check self.bounds with rotation!! origin not necessary equal to total_bounds
        west, south, _, _ = self.bounds
        if self.crs is not None:
            self.set_config("epsg", self.crs.to_epsg())
        self.set_config("mmax", self.width)
        self.set_config("nmax", self.height)
        self.set_config("dx", dx)
        self.set_config("dy", abs(dy))  # dy is always positive (orientation is S -> N)
        self.set_config("x0", west)
        self.set_config("y0", south)

    def update_grid_from_config(self):
        """Update grid properties based on `config` (sfincs.inp) attributes"""
        self.grid_type = (
            "quadtree" if self.config.get("qtrfile") is not None else "regular"
        )
        if self.grid_type == "regular":
            self.reggrid = RegularGrid(
                x0=self.config.get("x0"),
                y0=self.config.get("y0"),
                dx=self.config.get("dx"),
                dy=self.config.get("dy"),
                nmax=self.config.get("nmax"),
                mmax=self.config.get("mmax"),
                rotation=self.config.get("rotation", 0),
                epsg=self.config.get("epsg"),
            )
        else:
            raise not NotImplementedError("Quadtree grid not implemented yet")
            # self.quadtree = QuadtreeGrid()

    def get_spatial_attrs(self, crs=None):
        """Get geospatial `config` (sfincs.inp) attributes.

        Parameters
        ----------
        crs: int, CRS
            Coordinate reference system

        Returns
        -------
        shape: tuple of int
            width, height
        transform: Affine.transform
            Geospatial transform
        crs: pyproj.CRS
            Coordinate reference system
        """
        return utils.get_spatial_attrs(self.config, crs=crs, logger=self.logger)

    def get_model_time(self):
        """Return (tstart, tstop) tuple with parsed model start and end time"""
        tstart = utils.parse_datetime(self.config["tstart"])
        tstop = utils.parse_datetime(self.config["tstop"])
        return tstart, tstop

    ## helper method

    def _parse_datasets_dep(self, datasets_dep, res):
        """Parse filenames or paths of Datasets in list of dictionaries datasets_dep into xr.DataArray and gdf.GeoDataFrames:
        * dep_fn is parsed into da (xr.DataArray)
        * offset_fn is parsed into da_offset (xr.DataArray)
        * gdf_valid_fn is parsed into gdf (gpd.GeoDataFrame)

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (dep_fn) and optional merge arguments.
        res : float
            Resolution of the model grid in meters. Used to obtain the correct zoom level of the depth datasets.
        """        
        for dataset in datasets_dep:
            # read in depth datasets; replace da_fn for da
            dep_fn = dataset.get("dep_fn")
            da_elv = self.data_catalog.get_rasterdataset(
                dep_fn,
                geom=self.mask.raster.box,
                buffer=10,
                variables=["elevtn"],
                zoom_level=(res, "meter"),
            )
            dataset.update({"da": da_elv})

            # read offset filenames
            # NOTE offsets can be xr.DataArrays and floats
            offset_fn = dataset.get("offset_fn", None)
            if offset_fn is not None:
                da_offset = self.data_catalog.get_rasterdataset(
                    offset_fn,
                    geom=self.mask.raster.box,
                    buffer=20,
                )
                dataset.update({"offset": da_offset})

            # read geodataframes describing valid areas
            gdf_valid_fn = dataset.get("gdf_valid_fn", None)
            if gdf_valid_fn is not None:
                gdf_valid = self.data_catalog.get_geodataframe(
                    path_or_key=gdf_valid_fn,
                    geom=self.mask.raster.box,
                )
                dataset.update({"gdf_valid": gdf_valid})

        return datasets_dep

    def _parse_datasets_rgh(self, datasets_rgh):
        """Parse filenames or paths of Datasets in list of dictionaries datasets_rgh into xr.DataArrays and gdf.GeoDataFrames:
        * manning_fn is parsed into da (xr.DataArray)
        * lulc_fn is parsed into da (xr.DataArray) using reclassify table in map_fn
        * gdf_valid_fn is parsed into gdf (gpd.GeoDataFrame)

        Parameters
        ----------
        datasets_rgh : List[dict], optional
            List of dictionaries with Manning's n datasets. Each dictionary should at least contain one of the following:
            * (1) manning_fn: filename (or Path) of gridded data with manning values
            * (2) lulc_fn (and map_fn) :a combination of a filename of gridded landuse/landcover and a mapping table.
            In additon, optional merge arguments can be provided e.g.: merge_method, gdf_valid_fn 
        """    
        for dataset in datasets_rgh:
            manning_fn = dataset.get("manning_fn", None)
            # landuse/landcover should always be combined with mapping
            lulc_fn = dataset.get("lulc_fn", None)
            map_fn = dataset.get("map_fn", None)

            if manning_fn is not None:
                da_man = self.data_catalog.get_rasterdataset(
                    manning_fn,
                    geom=self.mask.raster.box,
                    buffer=10,
                )
                dataset.update({"da": da_man})
            elif lulc_fn is not None:
                if map_fn is None:
                    map_fn = join(DATADIR, "lulc", f"{lulc_fn}_mapping.csv")
                if not os.path.isfile(map_fn):
                    raise IOError(f"Manning roughness mapping file not found: {map_fn}")
                da_lulc = self.data_catalog.get_rasterdataset(
                    lulc_fn, geom=self.mask.raster.box, buffer=10, variables=["lulc"]
                )
                df_map = self.data_catalog.get_dataframe(map_fn, index_col=0)
                # reclassify
                da_man = da_lulc.raster.reclassify(df_map)["N"]

                dataset.update({"da": da_man})

            # read geodataframes describing valid areas
            gdf_valid_fn = dataset.get("gdf_valid_fn", None)
            if gdf_valid_fn is not None:
                gdf_valid = self.data_catalog.get_geodataframe(
                    path_or_key=gdf_valid_fn,
                    geom=self.mask.raster.box,
                )
                dataset.update({"gdf_valid": gdf_valid})

        return datasets_rgh
