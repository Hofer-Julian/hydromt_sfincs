.. currentmodule:: hydromt_sfincs.sfincs

============================
Model methods and components
============================

With the HydroMT SFINCS plugin, you can easily work with SFINCS model schematizations. 
This plugin helps you preparing or updating several model components of a SFINCS model 
such as topography/bathymetry, roughness, infiltration maps and dynamic waterlevel and 
discharge forcing.


.. _model_methods:

Model setup methods
===================

The SFINCS model methods are available from the HydroMT Command Line and Python Interfaces and 
allow you to configure HydroMT in order to build or update SFINCS model schematizations.

An overview of the available SfincsModel methods, workflows and low-level methods
is provided in the table below. Click on header to get a full overview or directly on
a specific method see its documentation.  

.. _general_table:

.. list-table:: General setup methods
   :widths: 20 25 25 30
   :header-rows: 1

   * - SFINCS file
     - :ref:`model setup methods <model_methods>`
     - :ref:`workflows <workflows>`
     - :ref:`low-level methods <methods>`
   * - model region
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_region`
     - :py:func:`~hydromt.workflows.parse_region` :py:func:`~hydromt.workflows.get_basin_geometry`
     - 
   * - sfincs.inp
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_config`
     - :py:func:`~hydromt.workflows.parse_region`:sup:`1` :py:func:`~hydromt.workflows.get_basin_geometry`:sup:`1`
     - :py:func:`~hydromt_sfincs.read_inp` :py:func:`~hydromt_sfincs.write_inp` :py:func:`~hydromt_sfincs.get_spatial_attrs`
   * - depfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_topobathy` :py:func:`~hydromt_sfincs.SfincsModel.setup_merge_topobathy` :py:func:`~hydromt_sfincs.SfincsModel.setup_river_bathymetry`
     - :py:func:`~hydromt_sfincs.workflows.merge_topobathy`
     - :py:func:`~hydromt_sfincs.read_binary_map` :py:func:`~hydromt_sfincs.write_binary_map`
   * - mskfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_mask` :py:func:`~hydromt_sfincs.SfincsModel.setup_bounds` :py:func:`~hydromt_sfincs.SfincsModel.setup_river_outflow`
     - :py:func:`~hydromt_sfincs.utils.mask_topobathy` :py:func:`~hydromt_sfincs.utils.mask_bounds`
     - :py:func:`~hydromt_sfincs.read_binary_map` :py:func:`~hydromt_sfincs.write_binary_map` 
   * - indfile
     - 
     - 
     - :py:func:`~hydromt_sfincs.read_binary_map_index` :py:func:`~hydromt_sfincs.write_binary_map_index`
   * - manningfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_manning_roughness`
     - :py:func:`~hydromt_sfincs.workflows.landuse`
     - :py:func:`~hydromt_sfincs.read_binary_map` :py:func:`~hydromt_sfincs.write_binary_map`
   * - scsfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_cn_infiltration`
     - :py:func:`~hydromt_sfincs.workflows.cn_to_s`
     - :py:func:`~hydromt_sfincs.read_binary_map` :py:func:`~hydromt_sfincs.write_binary_map`
   * - obsfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_gauges`
     -
     - :py:func:`~hydromt_sfincs.read_xy` :py:func:`~hydromt_sfincs.write_xy`
   * - thd- & weirfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_structures`
     -
     - :py:func:`~hydromt_sfincs.read_structures` :py:func:`~hydromt_sfincs.write_structures` :py:func:`~hydromt_sfincs.utils.gdf2structures` :py:func:`~hydromt_sfincs.utils.structures2gdf`

.. _forcing_table:

.. list-table:: Forcing setup methods
   :widths: 20 25 25 30
   :header-rows: 1

   * - SFINCS file
     - :ref:`model setup methods <model_methods>`
     - :ref:`workflows <workflows>`
     - :ref:`low-level methods <methods>`
   * - bnd- & bzsfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_h_forcing`
     -
     - :py:func:`~hydromt_sfincs.read_timeseries` :py:func:`~hydromt_sfincs.write_timeseries` :py:func:`~hydromt_sfincs.read_xy` :py:func:`~hydromt_sfincs.write_xy`
   * - src- & disfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_river_inflow` :py:func:`~hydromt_sfincs.SfincsModel.setup_q_forcing` :py:func:`~hydromt_sfincs.SfincsModel.setup_q_forcing_from_grid`
     - :py:func:`~hydromt_sfincs.workflows.snap_discharge`
     - :py:func:`~hydromt_sfincs.read_timeseries` :py:func:`~hydromt_sfincs.write_timeseries` :py:func:`~hydromt_sfincs.read_xy` :py:func:`~hydromt_sfincs.write_xy`
   * - precipfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_p_forcing` :py:func:`~hydromt_sfincs.SfincsModel.setup_p_forcing_from_grid`
     - :py:func:`~hydromt.workflows.resample_time`:sup:`1`
     - :py:func:`~hydromt_sfincs.read_timeseries` :py:func:`~hydromt_sfincs.write_timeseries`
   * - netamprfile
     - :py:func:`~hydromt_sfincs.SfincsModel.setup_p_forcing_from_grid`
     - :py:func:`~hydromt.workflows.resample_time`:sup:`1`
     -

:sup:`1`) Imported from hydromt core package

.. _model_components:

Model components
================

The following table provides an overview of which :py:class:`~hydromt_sfincs.SfincsModel` 
model data component (attribute) contains which SFINCS in- and output files. The files are read and written with the associated 
read- and write- methods, i.e. :py:func:`~hydromt_sfincs.sfincs.SfincsModel.read_config` 
and :py:func:`~hydromt_sfincs.sfincs.SfincsModel.write_config` for the 
:py:attr:`~hydromt_sfincs.sfincs.SfincsModel.config`  attribute. 

Note that the indfile is not part of the staticmaps dataset but created based on 
the mskfile upon writing and used for reading staticmaps.


.. list-table:: SfincsModel data component
   :widths: 30 70
   :header-rows: 1

   * - :py:class:`~hydromt_sfincs.SfincsModel` attribute
     - SFINCS files
   * - :py:attr:`~hydromt_sfincs.SfincsModel.config`
     - sfincs.inp
   * - :py:attr:`~hydromt_sfincs.SfincsModel.staticmaps`
     - depfile, mskfile, manningfile, qinffile, scsfile
   * - :py:attr:`~hydromt_sfincs.SfincsModel.staticgeoms`
     - obsfile, thdfile, weirfile
   * - :py:attr:`~hydromt_sfincs.SfincsModel.forcing`
     - bndfile, bzsfile, srcfile, disfile, precipfile, netamprfile
   * - :py:attr:`~hydromt_sfincs.SfincsModel.states`
     - inifile
   * - :py:attr:`~hydromt_sfincs.SfincsModel.results`
     - sfincs_his.nc, sfincs_map.nc



.. _data: https://deltares.github.io/hydromt/latest/user_guide/data_overview.html
.. _region: https://deltares.github.io/hydromt/latest/user_guide/model_region.html