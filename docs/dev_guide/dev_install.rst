.. _dev_env:

Developer's environment
=======================
If you want to download the SFINCS plugin directly from git to easily have access to the latest developments or
make changes to the code you can use the following steps.

First, clone the HydroMT SFINCS plugin ``git`` repo from
`github <https://github.com/Deltares/hydromt_sfincs>`_, then navigate into the
the code folder (where the envs folder and pyproject.toml are located):

.. code-block:: console

    $ git clone https://github.com/Deltares/hydromt_sfincs.git
    $ cd hydromt_sfincs

Then, make and activate a new hydromt-sfincs conda environment based on the envs/hydromt-sfincs.yml
file contained in the repository:

.. code-block:: console

    $ conda env create -f envs/hydromt-sfincs.yml
    $ conda activate hydromt-sfincs

Finally, to make changes in hydromt_sfincs, you should make an editable install of HydroMT.

.. code-block:: console

    $ pip install -e .
