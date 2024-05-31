# %%
# Name: 5A1_msw_landfills.py

# Authors Name: N. Kruskamp, H. Lohman (RTI International), Erin McDuffie (EPA/OAP)
# Date Last Modified: 5/30/2024
# Purpose: Spatially allocates methane emissions for source category 5A1 Municipal Solid Waste Landfills
#
# Input Files:
#      - State_MSW_LF_1990-2021.xlsx (State inventory)
#      - hh_scale-up_1990-2022_LA.xlsx (Subpart HH data used for national numbers)
#      - hh_OX_info_90-22.xlsx (Larger Subpart HH data download)
#      - landfilllmopdata.xlsx (Landfill Methane Outreach Program (LMOP) data published March 2024)
#      - all_ghgi_mappings.csv
#      - all_proxy_mappings.csv
# Output Files:
#      - f"{INDUSTRY_NAME}_ch4_kt_per_year.tif, f"{INDUSTRY_NAME}_ch4_emi_flux.tif"
# Notes:
# TODO: update to use facility locations from 2024 GHGI state inventory files
# TODO: include plotting functionaility
# TODO: include netCDF writting functionality

# ---------------------------------------------------------------------
# %% STEP 0. Load packages, configuration files, and local parameters

import calendar
import warnings
from pathlib import Path

import osgeo  # noqa
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.enums
import seaborn as sns
from IPython.display import display

# from pytask import Product, task
from rasterio.features import rasterize

from gch4i.config import (
    data_dir_path,
    ghgi_data_dir_path,
    global_data_dir_path,
    max_year,
    min_year,
    tmp_data_dir_path,
)
from gch4i.gridding import ARR_SHAPE, GEPA_PROFILE
from gch4i.utils import (
    calc_conversion_factor,
    load_area_matrix,
    name_formatter,
    tg_to_kt,
    write_ncdf_output,
    write_tif_output,
)
t_to_kt = 0.001

# %%

# https://www.epa.gov/system/files/documents/2024-02/us-ghg-inventory-2024-main-text.pdf
INDUSTRY_NAME = "5A1_waste_msw_landfills"

# State-level inventory data
EPA_inputfile = Path(ghgi_data_dir_path / "landfills" / "State_MSW_LF_1990-2021.xlsx")

# NOTE: this file uses the Envirofacts GHG Query Builder to retrieve
# Supart HH facility-level emissions and locations (latitude, longitude). We need to
# check with the sector leads to determine if other data should be used in addition
# to Subpart HH for facility-level emissions.
# Alternate data if API is not used: hh_scale-up_1990-2022_LA.xlsx, hh_OX_info_90-22.xlsx
EPA_ghgrp_msw_landfills_inputfile = "https://data.epa.gov/efservice/hh_subpart_level_information/pub_dim_facility/ghg_name/=/Methane/CSV"

# Facility-level emissions and location data from the Landfill Methane Outreach
# Program (LMOP). Represents a majority of MSW landfills (more than 2,600 MSW landfills
# that are either accepting MSW or closed in the past few decades).
LMOP_msw_landfills_inputfile = Path(ghgi_data_dir_path / "landfills" / "landfilllmopdata.xlsx")

# reference data paths
state_geo_path = global_data_dir_path / "landfills" / "tl_2020_us_state.zip"

# OUTPUT FILES (TODO: include netCDF flux files)
ch4_kt_dst_path = tmp_data_dir_path / f"{INDUSTRY_NAME}_ch4_kt_per_year.tif"
ch4_flux_dst_path = tmp_data_dir_path / f"{INDUSTRY_NAME}_ch4_emi_flux.tif"

area_matrix = load_area_matrix()

# %% STEP 1. Load GHGI-Proxy Mapping Files

# NOTE: looking at rework of the proxy mapping files into an aggregate flat file
# that would then be formed into a proxy dictionary to retain the existing approach
# but allow for interrogation of the objects when needed.

ghgi_map_path = data_dir_path / "all_ghgi_mappings.csv"
proxy_map_path = data_dir_path / "all_proxy_mappings.csv"
ghgi_map_df = pd.read_csv(ghgi_map_path)
proxy_map_df = pd.read_csv(proxy_map_path)

proxy_map_df.query("GHGI_Emi_Group == 'Emi_Petro'").merge(
    ghgi_map_df.query("GHGI_Emi_Group == 'Emi_Petro'"), on="GHGI_Emi_Group"
)

# %%
# read in the state shapefile to spatial join with facilities, assigning them to
# states for allocation of emissions
state_gdf = (
    gpd.read_file(state_geo_path)
    .loc[:, ["NAME", "STATEFP", "STUSPS", "geometry"]]
    .rename(columns=str.lower)
    .rename(columns={"stusps": "state_code", "name": "state_name"})
    .astype({"statefp": int})
    # get only lower 48 + DC
    .query("(statefp < 60) & (statefp != 2) & (statefp != 15)")
    .to_crs(4326)
)
state_gdf

# %%
# STEP 2: Read In EPA State GHGI Emissions by Year

# read in the ch4_kt values for each state
EPA_msw_landfills_emissions = (
    # read in the data
    pd.read_excel(
        EPA_inputfile,
        sheet_name="InvDB",
        skiprows=15,
        nrows=60,
        usecols="A:AO",
    )
    # name column names lower
    .rename(columns=lambda x: str(x).lower())
    # drop columns we don't need
    .drop(
        columns=[
            "sector",
            "source",
            "subsource",
            "fuel",
            "subref",
            "2nd ref",
            "exclude",
        ]
    )
    # get just methane emissions
    .query("ghg == 'CH4'")
    # remove that column
    .drop(columns="ghg")
    # set the index to state
    .set_index("state")
    # covert "NO" string to numeric (will become np.nan)
    .apply(pd.to_numeric, errors="coerce")
    # drop states that have all nan values
    .dropna(how="all")
    # reset the index state back to a column
    .reset_index()
    # make the table long by state/year
    .melt(id_vars="state", var_name="year", value_name="ch4_tg")
    .assign(ch4_kt=lambda df: df["ch4_tg"] * tg_to_kt)
    .drop(columns=["ch4_tg"])
    # make the columns types correcet
    .astype({"year": int, "ch4_kt": float})
    .fillna({"ch4_kt": 0})
    # get only the years we need
    .query("year.between(@min_year, 2021)")
    # .query("year.between(@min_year, @max_year)")
    .reset_index(drop=True)
)
EPA_msw_landfills_emissions.head()

# %% 
# QA/QC - check counts of years of state data and plot by state
display(EPA_msw_landfills_emissions["state"].value_counts())
display(EPA_msw_landfills_emissions["year"].min(), EPA_msw_landfills_emissions["year"].max())

# a quick plot to verify the values
sns.relplot(
    kind="line",
    data=EPA_msw_landfills_emissions,
    x="year",
    y="ch4_kt",
    hue="state",
    # legend=False,
)

# %%
# STEP 3: GET AND FORMAT PROXY DATA
# TODO: explore if it makes sense to write a function / task for every proxy.
# there are about 65 unique ones.
# EEM: this sounds like a good apprach. I'll leave that decision to RTI
def task_map_msw_landfills_proxy():
    pass


# The facilities have multiple reporting units for each year. This will read in the
# facilities data and compute the facility level sum of ch4_kt emissions for each
# year.

subpart_hh_df = pd.read_csv(
    EPA_ghgrp_msw_landfills_inputfile,
    usecols=("facility_name",
             "facility_id",
             "reporting_year",
             "ghg_quantity",
             "latitude",
             "longitude",
             "state",
             "zip"))
subpart_hh_df

# read in the SUMMARY facilities emissions data
msw_landfills_facilities_df = (
    pd.read_csv(
        EPA_ghgrp_msw_landfills_inputfile,
        # 13: facility name, 0: facility_id
        usecols=("facility_name",
                 "facility_id",
                 "reporting_year",
                 "ghg_quantity",
                 "latitude",
                 "longitude",
                 "state",
                 "zip"))
    .rename(columns=lambda x: str(x).lower())
    .rename(columns={"reporting_year": "year", "ghg_quantity": "ch4_t"})
    .assign(ch4_kt=lambda df: df["ch4_t"] * t_to_kt)
    .drop(columns=["ch4_t"])
    .drop_duplicates(subset=['facility_id', 'year'], keep='last')
    .astype({"year": int})
    # .query("year.between(@min_year, @max_year)")
    .query("year.between(@min_year, 2021)")
    .reset_index()
    .drop(columns='index')

)

msw_landfills_facilities_df.head()

msw_landfills_facilities_gdf = (
    gpd.GeoDataFrame(
        msw_landfills_facilities_df,
        geometry=gpd.points_from_xy(
            msw_landfills_facilities_df["longitude"],
            msw_landfills_facilities_df["latitude"],
            crs=4326,
        ),
    )
    .drop(columns=["latitude", "longitude"])
    .loc[:, ["facility_id", "facility_name", "state", "zip", "geometry", "year", "ch4_kt"]]
)
msw_landfills_facilities_gdf.head()

# %% QA/QC
# make sure the merge gave us the number of results we expected.
if not (msw_landfills_facilities_gdf.shape[0] == msw_landfills_facilities_df.shape[0]):
    print("WARNING the merge shape does not match the original data")
# %% # save a shapefile of the v3 ferro facilities for reference
fac_locations = msw_landfills_facilities_gdf.dissolve("facility_id")
fac_locations[fac_locations.is_valid].loc[:, ["geometry"]].to_file(
    tmp_data_dir_path / "v3_waste_msw_landfills_facilities.shp.zip", driver="ESRI Shapefile"
)

# %% QA/QC

# some checks of the data
# how many na values are there
print('Number of NaN values:')
display(msw_landfills_facilities_gdf.isna().sum())
# how many missing locations are there by year
print("Number of Facilities with Missing Locations Each Year")
display(msw_landfills_facilities_gdf[msw_landfills_facilities_gdf["zip"].isna()]["year"].value_counts())
# how many missing locations are there by facility name
print("For Each Facility with Missing Data, How Many Missing Years")
display(
    msw_landfills_facilities_gdf[msw_landfills_facilities_gdf["zip"].isna()][
        "facility_id"
    ].value_counts()
)
# a plot of the timeseries of emission by facility
sns.lineplot(
    data=msw_landfills_facilities_gdf, x="year", y="ch4_kt", hue="facility_id", legend=False
)

# %%

# STEP 4: ALLOCATION OF STATE / YEAR EMISSIONS TO EACH FACILITY
#         (BY PROXY FRACTION IN EACH GRIDCELL)
# For this source, state-level emissions are spatially allocated using the
#   the fraction of facility-level emissions within each grid cell in each state, 
#   for each year

# This does the allocation for us in a function by state and year.

# NOTE: HACL - This function breaks in the case of states having facilities in Subpart X, 
# but no emissions reported in the state inventory. Dummy data for these states has been
# created to allow this to run correctly, but it may need to be reworked to properly
# handle this case.


def state_year_allocation_emissions(fac_emissions, inventory_df):

    # fac_emissions are the total emissions for the facilities located in that state
    # and year. It can be one or more facilities. Inventory_df EPA state GHGI summary
    # emissions table

    # get the target state and year
    state, year = fac_emissions.name

    # get the total proxy data (e.g., emissions) within that state and year. 
    # It will be a single value.
    emi_sum = inventory_df[
        (inventory_df["state"] == state) & (inventory_df["year"] == year)
    ]["ch4_kt"].iat[0]

    # allocate the EPA GHGI state emissions to each individual facility based on their
    # proportion emissions (i.e., the fraction of total state-level emissions occuring at each facility)
    allocated_fac_emissions = ((fac_emissions / fac_emissions.sum()) * emi_sum).fillna(
        0
    )
    return allocated_fac_emissions


# we create a new column that assigns the allocated summary emissions to each facility
# based on its proportion of emission to the facility totals for that state and year.
# so for each state and year in the summary emissions we apply the function.
msw_landfills_facilities_gdf["allocated_ch4_kt"] = msw_landfills_facilities_gdf.groupby(
    ["state", "year"]
)["ch4_kt"].transform(state_year_allocation_emissions, inventory_df=EPA_msw_landfills_emissions)

msw_landfills_facilities_gdf.head()

# %% QA/QC
# We now check that the sum of facility emissions equals the EPA GHGI emissions by state
# and year. The resulting sum_check table shows you where the emissions data DO NOT
# equal and need more investigation.
# NOTE: currently we are missing facilities in states, so we also check below that the
# states that are missing emissions are the ones that are missing facilities.
sum_check = (
    msw_landfills_facilities_gdf.groupby(["state", "year"])["allocated_ch4_kt"]
    .sum()
    .reset_index()
    .merge(EPA_msw_landfills_emissions, on=["state", "year"], how="outer")
    .assign(
        check_diff=lambda df: df.apply(
            lambda x: np.isclose(x["allocated_ch4_kt"], x["ch4_kt"]), axis=1
        )
    )
)

with pd.option_context('display.max_rows', None, 'display.max_columns', None):
    display(sum_check[~sum_check["check_diff"]])

# NOTE: For now, facilities data are not final / missing. We don't have facilities in
# all the state summaries that are reporting, and we may be missing facilities even
# within states that are represented. If these lists match, we have a good idea of
# what is missing currently due to the preliminary data.

# NOTE: As of May 2024:
# States in the inventory with no Subpart X facilities: CO, DE, WY
# States with Subpart X facilities but are not in the inventory: IL, IA, AL, KS, KY, PA
# Dummy data was created in the inventory spreadsheet for IL, IA, AL, KS, KY, and PA
# to allow the allocation code to run and should be updated with the final data.
print(
    (
        "states with no facilities in them: "
        f"{EPA_msw_landfills_emissions[~EPA_msw_landfills_emissions['state'].isin(msw_landfills_facilities_gdf['state'])]['state'].unique()}"
    )
)

print(
    (
        "states with facilities in them but not accounted in state inventory: "
        f"{msw_landfills_facilities_gdf[~msw_landfills_facilities_gdf['state'].isin(EPA_msw_landfills_emissions['state'])]['state'].unique()}"
    )
)

print(
    (
        "states with unaccounted emissions: "
        f"{sum_check[~sum_check['check_diff']]['state'].unique()}"
    )
)

# %%

# STEP 5: RASTERIZE THE CH4 KT AND FLUX
#         e.g., calculate fluxes and place the facility-level emissions on the CONUS grid

# for each year, grid the adjusted emissions data in kt and do conversion for flux.
ch4_kt_result_rasters = {}
ch4_flux_result_rasters = {}

# NOTE: this warning filter is because we currently have facilities with missing
# geometries.
# TODO: remove this filter when full locations data are available.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    for year, data in msw_landfills_facilities_gdf.groupby("year"):

        # same results as summing the month days
        # if calendar.isleap(year):
        #     year_days = 366
        # else:
        #     year_days = 365
        month_days = [calendar.monthrange(year, x)[1] for x in range(1, 13)]
        year_days = np.sum(month_days)

        # TODO: check that when multiple points fall into the same cell, their values
        # are added together.
        ch4_kt_raster = rasterize(
            shapes=[
                (shape, value)
                for shape, value in data[["geometry", "allocated_ch4_kt"]].values
            ],
            out_shape=ARR_SHAPE,
            fill=0,
            transform=GEPA_PROFILE["transform"],
            dtype=np.float64,
            merge_alg=rasterio.enums.MergeAlg.add,
        )

        conversion_factor_annual = calc_conversion_factor(year_days, area_matrix)
        ch4_flux_raster = ch4_kt_raster * conversion_factor_annual

        ch4_kt_result_rasters[year] = ch4_kt_raster
        ch4_flux_result_rasters[year] = ch4_flux_raster
# --------------------------------------------------------------------------


# %%
# STEP 6: SAVE THE FILES

# check the sums all together now...
# TODO: report QC metrics for both kt and flux values

for year, raster in ch4_kt_result_rasters.items():
    raster_sum = raster.sum()
    fac_sum = msw_landfills_facilities_gdf.query("year == @year")["allocated_ch4_kt"].sum()
    emi_sum = EPA_msw_landfills_emissions.query("year == @year")["ch4_kt"].sum()
    missing_sum = sum_check[(~sum_check["check_diff"]) & (sum_check["year"] == year)][
        "ch4_kt"
    ].sum()

    print(year)
    print(
        "does the raster sum equal the facility sum: "
        f"{np.isclose(raster_sum, fac_sum)}"
    )
    print(
        "does the raster sum equal the national total: "
        f"{np.isclose(raster_sum, emi_sum)}"
    )
    # this shows we are consistent with our missing emissions where the states with no
    # facilities make up the difference, as we would expect.
    print(
        "do the states with no facilities equal the missing amount: "
        f"{np.isclose((emi_sum - raster_sum), missing_sum)}"
    )
    print()

# %% Write files

# EEM: TODO: add netCDF output
write_tif_output(ch4_kt_result_rasters, ch4_kt_dst_path)
write_tif_output(ch4_flux_result_rasters, ch4_flux_dst_path)
# ------------------------------------------------------------------------
# %%
# STEP 7: PLOT

# TODO: add map of output flux data

#%%

# END
