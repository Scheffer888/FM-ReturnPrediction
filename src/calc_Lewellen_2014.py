"""
This module calculates the Lewellen (2014) Table 1, Table 2 and Figure 1.

The Lewellen (2014) paper is available at:
https://faculty.tuck.dartmouth.edu/images/uploads/faculty/jonathan-lewellen/ExpectedStockReturns.pdf

"""
from datetime import datetime
from dateutil.relativedelta import relativedelta
from pathlib import Path
from typing import Union, List

import numpy as np
import pandas as pd
import polars as pl
from pandas.tseries.offsets import MonthEnd
import statsmodels.api as sm
import matplotlib.pyplot as plt

from settings import config
from utils import load_cache_data, _save_figure
from pull_compustat import pull_Compustat, pull_CRSP_Comp_link_table
from pull_crsp import pull_CRSP_stock, pull_CRSP_index
from transform_compustat import (expand_compustat_annual_to_monthly,
                                 add_report_date,
                                 merge_CRSP_and_Compustat,
                                 calc_book_equity
                                )
from transform_crsp import calculate_market_equity
from regressions import run_monthly_cs_regressions, fama_macbeth_summary
    

# ==============================================================================================
# GLOBAL CONFIGURATION
# ==============================================================================================

RAW_DATA_DIR = Path(config("RAW_DATA_DIR"))
OUTPUT_DIR = Path(config("OUTPUT_DIR"))
WRDS_USERNAME = config("WRDS_USERNAME")
START_DATE = config("START_DATE")
END_DATE = config("END_DATE")


def get_subsets(crsp_comp: pd.DataFrame) -> dict:
    """
    Given a monthly CRSP DataFrame with columns at least:
       ['mthcaldt', 'permno', 'me', 'primaryexch'],
    compute the NYSE 20th and 50th percentile of 'me' each month, store them
    in each row as 'me_20' and 'me_50', then build subset DataFrames:

      1) all_stocks          : everyone
      2) all_but_tiny_stocks : rows where me >= me_20
      3) large_stocks        : rows where me >= me_50

    If a particular month has no NYSE stocks (so me_20 or me_50 is NaN),
    then no rows from that month go into the 'all_but_tiny_stocks' or
    'large_stocks' subsets.

    Returns
    -------
    dict
        {
          "all_but_tiny_stocks":  <DataFrame of rows with me >= me_20>,
          "large_stocks":         <DataFrame of rows with me >= me_50>,
          "all_stocks":           crsp_comp   (the entire dataset)
        }
    """
    # 1) Sort for consistent grouping
    crsp_comp = crsp_comp.sort_values(["mthcaldt", "permno"]).copy()

    # 2) Compute month-specific me_20 and me_50 from NYSE
    #    group by mthcaldt, restrict to primaryexch == 'N'
    #    then get quantile(0.2) and quantile(0.5)
    nyse_me_percentiles = (
        crsp_comp
        .loc[crsp_comp["primaryexch"] == "N"]      # keep only NYSE rows
        .groupby("mthcaldt")["me"]
        .quantile([0.2, 0.5])                      # get 20th & 50th
        .unstack(level=1)                          # pivot so columns = [0.2, 0.5]
        .reset_index()
        .rename(columns={0.2: "me_20", 0.5: "me_50"})
    )
    # nyse_stats has columns ['mthcaldt', 'me_20', 'me_50']

    # 3) Merge these percentile columns back to crsp_comp
    crsp_comp = pd.merge(
        crsp_comp,
        nyse_me_percentiles,
        on="mthcaldt",
        how="left"
    )

    # 4) Create boolean columns for "all_but_tiny" and "large"
    #    If me_20 or me_50 is NaN (month has no NYSE?), these will be False
    crsp_comp["is_all_but_tiny"] = crsp_comp["me"] >= crsp_comp["me_20"]
    crsp_comp["is_large"]        = crsp_comp["me"] >= crsp_comp["me_50"]

    # 5) Now build the dictionary of DataFrames
    all_stocks_df = crsp_comp.copy()

    # For "all_but_tiny", we keep only rows with is_all_but_tiny == True
    all_but_tiny_df = crsp_comp.loc[crsp_comp["is_all_but_tiny"] == True].copy()

    # For "large_stocks", keep only rows with is_large == True
    large_stocks_df = crsp_comp.loc[crsp_comp["is_large"] == True].copy()

    subsets_crsp_comp = {
        "All stocks":          all_stocks_df,
        "All-but-tiny stocks": all_but_tiny_df,
        "Large stocks":        large_stocks_df,
    }
    return subsets_crsp_comp


"""
In the functions below:
Calculate the fundamentals for each firm in the Compustat dataset.
    log_size: Log market value of equity at the end of the prior month
    log_bm: Log book value of equity minus log market value of equity at the end of the prior month
    return_12_2:  Stock return from month -12 to month -2
    accruals: Change in non-cash net working capital minus depreciation in the prior fiscal year
    log_issues_36: Log growth in split-adjusted shares outstanding from month -36 to month -1
    roa: Income before extraordinary items divided by average total assets in the prior fiscal year
    log_assets_growth: Log growth in total assets in the prior fiscal year
    dy: Dividends per share over the prior 12 months divided by price at the end of the prior month
    log_return_13_36: Log stock return from month -13 to month -36
    log_issues_12: Log growth in split-adjusted shares outstanding from month -12 to month -1
    beta_36: Rolling beta (market sensitivity) for each stock, estimated from weekly returns from month -36 to month -1
    std_12: Monthly standard deviation, estimated from daily returns from month -12 to month -1
    debt_price: Short-term plus long-term debt divided by market value at the end of the prior month
    sales_price: Sales in the prior fiscal year divided by market value at the end of the prior month

Accounting data are assumed to be known four months after the end of the fiscal year as calculated in add_report_date(comp) function.
""" 


def calc_log_size(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate log market value of equity at the end of the prior month.
    For each (permno, month), we take 'me' from the previous month and log it.
    A new column 'log_size' is created.
    """
    # Shift 'me' by 1 month within each permno
    crsp_comp["me_lag"] = crsp_comp.groupby("permno")["me"].shift(1)
    crsp_comp["log_size"] = np.log(crsp_comp["me_lag"])
    crsp_comp = crsp_comp.drop(columns=["me_lag"])

    return crsp_comp

def calc_log_bm(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate log book-to-market ratio = ln(BE_{t-1}) - ln(ME_{t-1}).
    For each (permno, month), we shift 'be' and 'me' by 1 month, then take logs.
    A new column 'log_bm' is created.
    """
    crsp_comp["be_lag"] = crsp_comp.groupby("permno")["be"].shift(1)
    crsp_comp["me_lag"] = crsp_comp.groupby("permno")["me"].shift(1)

    crsp_comp["log_bm"] = np.log(crsp_comp["be_lag"]) - np.log(crsp_comp["me_lag"])
    
    crsp_comp = crsp_comp.drop(columns=["be_lag", "me_lag"])

    return crsp_comp


def calc_return_12_2(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate the cumulative return from month -12 to month -2 for each t.
    We skip the last month (t-1) and compound returns from t-12 through t-2.
    Creates a new column 'return_12_2'.
    """
    # Shift returns by 2 so that row t sees ret_{t-2} in 'retx_shift2'
    crsp_comp["retx_shift2"] = crsp_comp.groupby("permno")["retx"].shift(2)

    # We'll compute rolling product of 11 monthly returns: t-12..t-2
    # For each permno, we do a rolling(11) on (1 + retx_shift2).
    crsp_comp["1_plus_ret"] = 1 + crsp_comp["retx_shift2"]

    # Rolling product (min_periods=11 ensures we only compute when we have 11 data points)
    crsp_comp["rollprod_11"] = (
        crsp_comp
        .groupby("permno")["1_plus_ret"]
        .rolling(window=11, min_periods=11)
        .apply(np.prod, raw=True)
        .reset_index(level=0, drop=True)
    )

    crsp_comp["return_12_2"] = crsp_comp["rollprod_11"] - 1

    crsp_comp.drop(["retx_shift2", "1_plus_ret", "rollprod_11"], axis=1, inplace=True)

    return crsp_comp


def calc_accruals(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate change in non-cash net working capital minus depreciation
    in the prior fiscal year.
    We assume the monthly row already contains the correct accruals and
    depreciation for that month (e.g., forward-filled from annual data).
    Creates a new column 'accruals_final'.
    """
    crsp_comp["accruals_final"] = crsp_comp["accruals"] - crsp_comp["depreciation"]
    return crsp_comp


def calc_log_issues_36(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate log growth in split-adjusted shares outstanding from t-36 to t-1:
        ln(shrout_{t-1}) - ln(shrout_{t-36})
    Creates a new column 'log_issues_36'.
    """
    crsp_comp["shrout_t1"] = crsp_comp.groupby("permno")["shrout"].shift(1)
    crsp_comp["shrout_t36"] = crsp_comp.groupby("permno")["shrout"].shift(36)

    crsp_comp["log_issues_36"] = (
        np.log(crsp_comp["shrout_t1"]) - np.log(crsp_comp["shrout_t36"])
    )
    crsp_comp.drop(["shrout_t1", "shrout_t36"], axis=1, inplace=True)

    return crsp_comp


def calc_log_issues_12(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate log growth in shares outstanding from t-12 to t-1:
        ln(shrout_{t-1}) - ln(shrout_{t-12}).
    Creates a new column 'log_issues_12'.
    """
    crsp_comp["shrout_t1"] = crsp_comp.groupby("permno")["shrout"].shift(1)
    crsp_comp["shrout_t12"] = crsp_comp.groupby("permno")["shrout"].shift(12)

    crsp_comp["log_issues_12"] = (
        np.log(crsp_comp["shrout_t1"]) - np.log(crsp_comp["shrout_t12"])
    )
    crsp_comp.drop(["shrout_t1", "shrout_t12"], axis=1, inplace=True)

    return crsp_comp


def calc_roa(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate ROA as (income before extraordinary items) / (average total assets) in the prior FY.
    We assume 'roa' or 'earnings' and 'assets' are already properly merged in each monthly row.
    Creates a new column 'roa'.
    """
    # For illustration, if not already done:
    crsp_comp["roa"] = crsp_comp["earnings"] / crsp_comp["assets"]  # or average if you have that
    return crsp_comp


def calc_log_assets_growth(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate log growth in total assets from prior year: ln(assets_t / assets_{t-1}).
    We assume monthly data has the correct 'assets' for that month, forward-filled from the annual date.
    Creates a new column 'log_assets_growth'.
    """
    crsp_comp["lag_assets"] = crsp_comp.groupby("permno")["assets"].shift(12)  # or shift(1) if truly each year is only 12 months apart

    crsp_comp["log_assets_growth"] = np.log(crsp_comp["assets"] / crsp_comp["lag_assets"])
    crsp_comp.drop("lag_assets", axis=1, inplace=True)
    return crsp_comp


def calc_dy(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate dividend yield = (sum of dividends over prior 12 months) / price_{t-1}.
    We assume 'dvc' is the monthly dividend in month t, 'prc' is the end-of-month price.
    Creates a new column 'dy'.
    """
    df = crsp_comp.sort_values(["permno", "mthcaldt"]).copy()

    # Rolling sum of dividends over last 12 months
    df["div12"] = (
        df.groupby("permno")["dvc"]
        .rolling(window=12, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
    )

    # Price at t-1
    df["prc_t1"] = df.groupby("permno")["prc"].shift(1)
    df["dy"] = df["div12"] / df["prc_t1"]

    df.drop(["div12", "prc_t1"], axis=1, inplace=True)

    return df


def calc_log_return_13_36(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate the sum of log monthly returns over [t-36, t-13].
    For each month t, we skip last 12 months and sum the logs from t-36..t-13.
    Creates a new column 'log_return_13_36'.
    """

    # log(1 + retx)
    crsp_comp["log1p_ret"] = np.log(1 + crsp_comp["retx"])

    # shift by 13, then sum 24 rolling
    crsp_comp["log1p_ret_shift13"] = crsp_comp.groupby("permno")["log1p_ret"].shift(13)
    crsp_comp["log_sum_24"] = (
        crsp_comp.groupby("permno")["log1p_ret_shift13"]
        .rolling(window=24, min_periods=24)
        .sum()
        .reset_index(level=0, drop=True)
    )

    crsp_comp["log_return_13_36"] = crsp_comp["log_sum_24"]

    crsp_comp.drop(["log1p_ret", "log1p_ret_shift13", "log_sum_24"], axis=1, inplace=True)

    return crsp_comp


def calc_debt_price(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate (short-term + long-term debt) / market equity at end of prior month.
    Creates a new column 'debt_price'.
    """
    crsp_comp["me_lag"] = crsp_comp.groupby("permno")["me"].shift(1)

    crsp_comp["debt_price"] = crsp_comp["total_debt"] / crsp_comp["me_lag"]

    crsp_comp.drop("me_lag", axis=1, inplace=True)

    return crsp_comp


def calc_sales_price(crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate (sales) / market value at the end of prior month.
    Creates a new column 'sales_price'.
    """
    crsp_comp["me_lag"] = crsp_comp.groupby("permno")["me"].shift(1)

    crsp_comp["sales_price"] = crsp_comp["sales"] / crsp_comp["me_lag"]

    crsp_comp.drop("me_lag", axis=1, inplace=True)

    return crsp_comp


def calculate_rolling_beta(crsp_d: pd.DataFrame,
                           crsp_index_d: pd.DataFrame,
                           crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate rolling beta from weekly returns for each stock, estimated
    over the past 36 months (~156 weeks).

    Parameters
    ----------
    crsp_d : pd.DataFrame
        Must have 'dlycaldt' (daily date) and 'retx' (stock daily returns).
    crsp_index_d : pd.DataFrame
        Must have 'caldt' (daily date) and 'vwretx' (daily market returns).
    crsp_comp : pd.DataFrame
        Must have 'permno' (stock identifier) and 'mthcaldt' (month-end date).
    
    Returns
    -------
    pd.DataFrame
        index = last weekly date (which we'll eventually map to a month-end),
        columns = permno,
        values = rolling beta
    """

    df = crsp_d[['permno', 'dlycaldt', 'retx']].copy()
    mkt = crsp_index_d[["caldt","vwretx"]].copy()

    # Rename columns
    df = df.rename(columns={'retx': 'Ri', 'dlycaldt': 'date'})
    mkt = mkt.rename(columns={'vwretx': 'Rm', 'caldt': 'date'})

    # Convert to Polars DataFrame
    df_pl = pl.DataFrame(df)
    mkt_pl = pl.DataFrame(mkt)

    # Join on "date"
    df_joined = df_pl.join(mkt_pl, on="date")

    # Create log-returns (log_Ri, log_Rm) = log(1 + Ri), log(1 + Rm)
    df_joined = df_joined.with_columns([
        (pl.col("Ri") + 1).log().alias("log_Ri"),
        (pl.col("Rm") + 1).log().alias("log_Rm")
    ])

    # Sort by date
    df_joined = df_joined.sort(["permno", "date"])

    # Convert to a LazyFrame to use groupby_rolling
    lazy_df = df_joined.lazy()

    # Use groupby_rolling to aggregate over a 156-week window, grouped by permno.
    # This computes the rolling partial sums needed to approximate beta in log-return space.
    df_beta_lazy = (
        lazy_df
        .group_by_dynamic(
            index_column="date",
            every="1w",
            period="156w", 
            by="permno"
        )
        .agg([
            pl.col("log_Ri").sum().alias("sum_Ri"),
            pl.col("log_Rm").sum().alias("sum_Rm"),
            (pl.col("log_Ri") * pl.col("log_Rm")).sum().alias("sum_RiRm"),
            (pl.col("log_Rm") ** 2).sum().alias("sum_Rm2"),
            pl.count().alias("count_obs"),
        ])
        # Compute beta from the aggregated sums.
        # Using the formula:
        #   beta = [sum_RiRm - (sum_Ri * sum_Rm / N)] / [sum_Rm2 - (sum_Rm^2 / N)]
        .with_columns([
        (
            (pl.col("sum_RiRm") - (pl.col("sum_Ri") * pl.col("sum_Rm") / pl.col("count_obs")))
            /
            (pl.col("sum_Rm2") - (pl.col("sum_Rm")**2 / pl.col("count_obs")))
        ).alias("beta")
        ])
    )

    # Collect the results into an eager DataFrame.
    df_beta_pl = df_beta_lazy.collect()

    # (Optional) Convert back to a pandas DataFrame if needed.
    df_beta = df_beta_pl.to_pandas()

    df_beta['jdate'] = pd.to_datetime(df_beta['date']).dt.to_period('M').dt.to_timestamp('M')
    df_beta.drop_duplicates(subset=['permno', 'jdate'], keep='last', inplace=True)
    
    crsp_comp =  pd.merge(left=crsp_comp, right=df_beta[['permno', 'jdate', 'beta']], on=['permno', 'jdate'], how='left')

    return crsp_comp



def calc_std_12(crsp_d: pd.DataFrame, crsp_comp: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate monthly standard deviation from daily returns over the past ~12 months (252 trading days).
    This is the only function that works on daily data. We then merge the monthly result back.
    Creates a monthly DataFrame of stdevs, then you can merge to your monthly 'crsp_comp'.
    """
    
    df_std_12 = crsp_d.copy()

    # 252-day rolling std
    df_std_12["rolling_std_252"] = (
        df_std_12.groupby("permno")["retx"]
        .rolling(window=252, min_periods=100)
        .std()
        .reset_index(level=0, drop=True)
    )

    # Annualize:
    df_std_12["rolling_std_252"] = df_std_12["rolling_std_252"] * np.sqrt(252)

    # 3) For each month-end, pick the last available daily std in that month
    #    We'll create 'month_end' for daily data, then do a groupby last.
    df_std_12["jdate"] = df_std_12["dlycaldt"].dt.to_period("M").dt.to_timestamp("M")
    df_std_12.drop_duplicates(subset=['permno', 'jdate'], keep='last', inplace=True)
    
    crsp_comp =  pd.merge(left=crsp_comp, right=df_std_12[['permno', 'jdate', 'rolling_std_252']], on=['permno', 'jdate'], how='left')

    return crsp_comp


def filter_companies_table1(crsp_comp: pd.DataFrame, needed_var: list = None) -> set:
    """
    Identify companies that do NOT fit the criteria.

    A company fits the criteria if, for each required variable, it has at least one nonmissing value.
    That is, if for any required variable a company has all missing values, it does NOT fit the criteria:
      - current-month returns (Return, %)
      - beginning-of-month size (Log Size (-1))
      - book-to-market (Log B/M (-1))
      - lagged 12-month returns (Return (-2, -12))

    That is, it removes companyes with all missing values for any of these variables.
    Returns
    -------
    set
        A set of permnos for companies that have all missing values in any one of the required variables.
    """
    if needed_var is None:
        needed_vars = ["retx", "log_size", "log_bm", "return_12_2"]
    else:
        needed_vars = needed_var

    # For each company, check if there is any variable for which all values are missing.
    def has_all_missing(group):
        # group[needed_vars].isna().all() returns a boolean Series per column
        # .any() returns True if any column is entirely missing.
        return group[needed_vars].isna().all().any()

    # Group by company identifier and apply the check.
    flag_series = crsp_comp.groupby("permno").apply(has_all_missing)

    # Companies flagged True have at least one required variable completely missing.
    not_fitting = set(flag_series[flag_series].index)

    return not_fitting


def winsorize(crsp_comp: pd.DataFrame,
                   varlist: list,
                   lower_percentile=1,
                   upper_percentile=99) -> pd.DataFrame:
    """
    Winsorize the columns in `varlist` at [lower_percentile%, upper_percentile%], 
    cross-sectionally *by month* in the long DataFrame.
    Modifies the columns in place.
    """
    df = crsp_comp.sort_values(["mthcaldt", "permno"]).copy()

    for var in varlist:
        # Group by month, compute percentiles for that month
        def _winsorize_subgroup(subdf: pd.DataFrame):
            vals = subdf[var].dropna()
            if len(vals) < 5:
                return subdf  # Not enough data to reliably compute percentiles
            low_val = np.percentile(vals, lower_percentile)
            high_val = np.percentile(vals, upper_percentile)
            subdf[var] = subdf[var].clip(lower=low_val, upper=high_val)
            return subdf

        df = df.groupby("mthcaldt", group_keys=False).apply(_winsorize_subgroup)

    return df

def get_factors(crsp_comp: pd.DataFrame, crsp_d: pd.DataFrame,  crsp_index_d: pd.DataFrame):
    
    # Calculate all variables
    crsp_comp = crsp_comp.sort_values(["permno", "mthcaldt"])
    crsp_d = crsp_d.sort_values(["permno", "dlycaldt"])
    crsp_index_d = crsp_index_d.sort_values(["caldt"])
    
    crsp_comp = calc_log_size(crsp_comp)
    crsp_comp = calc_log_bm(crsp_comp)
    crsp_comp = calc_return_12_2(crsp_comp)
    crsp_comp = calc_accruals(crsp_comp)  # or calc_accruals(crsp_comp) if you have them merged
    crsp_comp       = calc_roa(crsp_comp)
    crsp_comp = calc_log_assets_growth(crsp_comp)
    crsp_comp = calc_dy(crsp_comp)
    crsp_comp = calc_log_return_13_36(crsp_comp)
    crsp_comp = calc_log_issues_12(crsp_comp)
    crsp_comp = calc_log_issues_36(crsp_comp)
    crsp_comp = calc_debt_price(crsp_comp)
    crsp_comp = calc_sales_price(crsp_comp)
    crsp_comp = calc_std_12(crsp_d, crsp_comp)
    crsp_comp = calculate_rolling_beta(crsp_d, crsp_index_d, crsp_comp)

    # Winsorize the variables to remove outliers
    factors_dict = {
    "Return (%)":                "retx",                # Assuming you are keeping this column name
    "Log Size (-1)":             "log_size",
    "Log B/M (-1)":              "log_bm",
    "Return (-2, -12)":          "return_12_2",
    "Log Issues (-1,-12)":       "log_issues_12",
    "Accruals (-1)":             "accruals_final",
    "ROA (-1)":                  "roa",
    "Log Assets Growth (-1)":    "log_assets_growth",
    "Dividend Yield (-1,-12)":   "dy",
    "Log Return (-13,-36)":      "log_return_13_36",
    "Log Issues (-1,-36)":       "log_issues_36",
    "Beta (-1,-36)":             "rolling_beta",
    "Std Dev (-1,-12)":          "rolling_std_252",
    "Debt/Price (-1)":           "debt_price",
    "Sales/Price (-1)":          "sales_price",
    }

    crsp_comp = winsorize(crsp_comp, factors_dict.values())

    return crsp_comp, factors_dict


def build_table_1(subsets_crsp_comp: dict, 
                  variables_dict: dict) -> pd.DataFrame:
    """
    Build a Table 1 with MultiIndex columns. For each subset in subsets_crsp_comp,
    we calculate time-series average of monthly cross-sectional stats for each variable.

    subsets_crsp_comp : dict
      {
         "All stocks": <DataFrame>,
         "All-but-tiny stocks": <DataFrame>,
         "Large stocks": <DataFrame>
      }

    variables_dict : dict
      {
        "Return (%)": "retx",
        "Log Size (-1)": "log_size",
        ...
      }

    Returns
    -------
    pd.DataFrame
        A table with one row per variable in `columns_of_interest`. Columns:
          - 'Avg':    The time-series average of the monthly cross-sectional means
          - 'Std':    The time-series average of the monthly cross-sectional stds
          - 'N':      The total number of unique permnos (distinct stocks) that appear for that variable in
    """

    subset_tables = {}  # We'll store a partial table for each subset

    for subset_name, df_subset in subsets_crsp_comp.items():
        rows = []
        
        for var_label, var_col in variables_dict.items():
            # 1) Keep only relevant columns
            if var_col not in df_subset.columns:
                # If for some reason this column doesn't exist, skip or fill with NaN
                rows.append({
                    "Column": var_label,
                    "Avg": np.nan,
                    "Std": np.nan,
                    "N":   np.nan
                })
                continue

            df_clean = df_subset[[var_col, "mthcaldt", "permno"]].copy()
            # 2) Replace inf with NaN, drop rows with NaN in var_col
            df_clean.replace([np.inf, -np.inf], np.nan, inplace=True)
            df_clean.dropna(subset=[var_col], inplace=True)

            if df_clean.empty:
                rows.append({
                    "Column": var_label,
                    "Avg": np.nan,
                    "Std": np.nan,
                    "N":   np.nan
                })
                continue

            # 3) Group by month, compute cross-sectional mean, std
            monthly_stats = df_clean.groupby("mthcaldt")[var_col].agg(["mean", "std"])
            # 4) Time-series average of monthly means, monthly std
            avg_mean = monthly_stats["mean"].mean()
            avg_std  = monthly_stats["std"].mean()

            # 5) N as total distinct permnos in the entire subset (like your friend’s example)
            N = df_clean["permno"].nunique()

            rows.append({"Column": var_label, "Avg": avg_mean, "Std": avg_std, "N": N})

        # Build a partial DataFrame for this subset
        partial_df = pd.DataFrame(rows).set_index("Column")
        # We'll store it
        subset_tables[subset_name] = partial_df

    # Now we merge them side-by-side with MultiIndex columns
    # Example: top-level is subset_name, second-level is [Avg, Std, N]
    partial_dfs = []
    for subset_name, partial_df in subset_tables.items():
        # Rename columns with a MultiIndex
        partial_df.columns = pd.MultiIndex.from_product([
            [subset_name],
            partial_df.columns
        ])
        partial_dfs.append(partial_df)

    # Concatenate along columns (axis=1)
    final_df = pd.concat(partial_dfs, axis=1)

    # Sort columns in a nice order if needed
    # final_df = final_df.reindex(columns=["All stocks", "All-but-tiny stocks", "Large stocks"], level=0)

    return final_df



def build_table_2(subsets_comp_crsp: dict, variables_dict: dict) -> pd.DataFrame:
    """
    Recreates Lewellen (2014) Table 2 style regressions:
      - Runs monthly cross-sectional regressions on three subsets:
          [All stocks, All-but-tiny stocks, Large stocks]
      - Uses three Models:
          1) Model 1: Three Predictors
          2) Model 2: Seven Predictors
          3) Model 3: Fourteen Predictors
      - Collects Fama-MacBeth slope, t-stat, and average R^2 for each predictor
        and arranges them in a wide table with top-level columns as subsets
        and second-level columns as (Slope, t-stat, R²). For each model and subset,
        R² is only displayed on the first predictor row. Additionally, numeric
        formatting is applied: predictor slopes and t-stats with 2 decimals, R² with
        3 decimals, and N as a whole number with a comma separator.

    Parameters
    ----------
    subsets_comp_crsp : dict of {str: pd.DataFrame}
        {
          "All stocks":          <DataFrame>,
          "All-but-tiny stocks": <DataFrame>,
          "Large stocks":        <DataFrame>,
        }
    variables_dict : dict
        Maps display names to actual column names, e.g.:
            {
                "Log Size (-1)": "log_size",
                "Log B/M (-1)":  "log_bm",
                ...
            }

    Returns
    -------
    pd.DataFrame
        A formatted DataFrame with MultiIndex columns = [Subset, (Slope, t-stat, R²)]
        and rows = (Model, Predictor) in the desired order.
    """
    # Define each model and the exact order of predictors.
    # We'll also append "N" at the bottom of each model block.
    models_predictors = {
        "Model 1: Three Predictors": [
            "Log Size (-1)",
            "Log B/M (-1)",
            "Return (-2, -12)",
        ],
        "Model 2: Seven Predictors": [
            "Log Size (-1)",
            "Log B/M (-1)",
            "Return (-2, -12)",
            "Log Issues (-1,-36)",
            "Accruals (-1)",
            "ROA (-1)",
            "Log Assets Growth (-1)",
        ],
        "Model 3: Fourteen Predictors": [
            "Log Size (-1)",
            "Log B/M (-1)",
            "Return (-2, -12)",
            "Log Issues (-1,-12)",
            "Accruals (-1)",
            "ROA (-1)",
            "Log Assets Growth (-1)",
            "Dividend Yield (-1,-12)",
            "Log Return (-13,-36)",
            "Log Issues (-1,-36)",
            "Beta (-1,-36)",
            "Std Dev (-1,-12)",
            "Debt/Price (-1)",
            "Sales/Price (-1)",
        ],
    }

    # Accumulate rows in a list of dicts.
    table_rows = []

    # 1) For each model.
    for model_name, pred_list in models_predictors.items():
        # For each subset of the data.
        for subset_name, df_sub in subsets_comp_crsp.items():
            # Build the list of actual X columns from variables_dict.
            xvars = []
            for lbl in pred_list:
                if lbl not in variables_dict:
                    raise ValueError(f"'{lbl}' not found in variables_dict!")
                xvars.append(variables_dict[lbl])

            # -- 2) Run monthly cross-sectional regressions --
            monthly_res = run_monthly_cs_regressions(
                df=df_sub,
                return_col="retx",
                predictor_cols=xvars,
                date_col="mthcaldt"
            )

            # -- 3) Fama-MacBeth summary (provides slope_xxx, tstat_xxx, mean_R2, mean_N, etc.) --
            fm_summary = fama_macbeth_summary(monthly_res, xvars, date_col="mthcaldt", nw_lags=4)

            # -- 4) Build table rows for each predictor in the desired order --
            for lbl, xcol in zip(pred_list, xvars):
                slope_val = fm_summary.get(f"{xcol}_coef",  np.nan)
                tstat_val = fm_summary.get(f"{xcol}_tstat", np.nan)
                r2_val    = fm_summary.get("mean_R2",       np.nan)
                table_rows.append({
                    "Model":     model_name,
                    "Predictor": lbl,
                    "Subset":    subset_name,
                    "Slope":     slope_val,
                    "t-stat":    tstat_val,
                    "R^2":       r2_val,
                })

            # 5) Also add a row for 'N' at the end of each model block.
            n_val = fm_summary.get("mean_N", np.nan)
            table_rows.append({
                "Model":     model_name,
                "Predictor": "N",
                "Subset":    subset_name,
                "Slope":     n_val,  # N is stored here.
                "t-stat":    np.nan,
                "R^2":       np.nan,
            })

    # 6) Convert the list of rows into a DataFrame.
    df_out = pd.DataFrame(table_rows)

    # 7) Pivot: rows = (Model, Predictor), columns = Subset, values = (Slope, t-stat, R^2).
    df_pivot = df_out.pivot(
        index=["Model", "Predictor"],
        columns="Subset",
        values=["Slope", "t-stat", "R^2"]
    )

    # 8) Swap levels so that the top level is the subsets and the second level is the metric.
    df_pivot = df_pivot.swaplevel(0, 1, axis=1)

    # 9) Reorder columns: subsets in a standard left-to-right sequence and metrics in the desired order.
    subset_order = ["All stocks", "All-but-tiny stocks", "Large stocks"]
    metric_order = ["Slope", "t-stat", "R^2"]

    df_pivot = df_pivot.reindex(labels=subset_order, axis=1, level=0)
    df_pivot = df_pivot.reindex(labels=metric_order, axis=1, level=1)

    # 10) Build a list of (Model, Predictor) pairs in the EXACT order we want
    #     to reindex the rows, so that "N" is last in each model block.
    row_order = []
    for model_name, pred_list in models_predictors.items():
        for lbl in pred_list:
            row_order.append((model_name, lbl))
        row_order.append((model_name, "N"))

    df_pivot = df_pivot.reindex(row_order)

    # 11) For each model, blank out the repeated R^2 values so that only the first predictor row shows R^2.
    for model, group in df_pivot.groupby(level="Model"):
        idx = group.index
        if len(idx) > 1:
            for subset in subset_order:
                df_pivot.loc[idx[1:], (subset, "R^2")] = ""

    # 12) Replace any remaining NaN with blank strings.
    df_pivot = df_pivot.fillna("")

    # 13) Format numeric values:
    #     - For predictor rows: "Slope" and "t-stat" with 2 decimals, "R^2" with 3 decimals.
    #     - For the "N" row (stored in the "Slope" column), display as whole number with a comma separator.
    formatted_df = df_pivot.copy()
    for row in formatted_df.index:
        model, predictor = row
        for col in formatted_df.columns:
            subset, metric = col
            cell_val = formatted_df.loc[row, col]
            if cell_val == "":
                continue
            try:
                # Try to convert the value to float.
                num_val = float(cell_val)
            except Exception:
                num_val = None

            if num_val is None:
                formatted_val = cell_val
            else:
                if predictor == "N" and metric == "Slope":
                    formatted_val = f"{int(round(num_val)):,.0f}"
                else:
                    if metric in ["Slope", "t-stat"]:
                        formatted_val = f"{num_val:.3f}"
                    elif metric == "R^2":
                        formatted_val = f"{num_val:.3f}"
                    else:
                        formatted_val = cell_val
            formatted_df.loc[row, col] = formatted_val

    return formatted_df


def create_figure_1(subsets_comp_crsp: dict,
                    save_plot: bool = True,
                    output_dir: Union[None, Path] = OUTPUT_DIR) -> tuple:
    """
    Creates Figure 1 with two vertically stacked panels (Panel A: all_stocks,
    Panel B: large_stocks), plotting ten-year rolling averages of Fama-MacBeth 
    slopes from Model 2. The legend labels use descriptive text.
    
    The plotted lines now begin flush against the y-axis.
    """
    # Model 2 variables (actual DF column names)
    model2_vars = ["log_bm", "return_12_2", "log_issues_36",
                "accruals_final", "log_assets_growth"]

    # Mapping actual column names to descriptive legend labels
    var_labels = {
        "log_bm":            "B/M",
        "return_12_2":       "Ret12",
        "log_issues_36":     "Issue36",
        "accruals_final":    "Accruals",
        "log_assets_growth": "Log AG"
    }

    # Dictionary to collect monthly slope DataFrames
    slopes_dict = {}

    # Loop over the two subsets: "all_stocks" and "large_stocks"
    for subset_name in ["All stocks", "Large stocks"]:
        if subset_name not in subsets_comp_crsp:
            continue

        df_sub = subsets_comp_crsp[subset_name].copy()
        df_sub = df_sub.sort_values(["mthcaldt", "permno"])
        df_sub = df_sub.dropna(subset=["retx"] + model2_vars)
        if df_sub.empty:
            continue

        monthly_slopes = []
        # Run cross-sectional regression for each month
        for mth, grp in df_sub.groupby("mthcaldt"):
            y = grp["retx"]
            X = grp[model2_vars]
            X = sm.add_constant(X, has_constant="add")
            if len(X) < len(model2_vars) + 1:
                continue
            
            model = sm.OLS(y, X, missing='drop')
            result = model.fit()
            slope_row = {"mthcaldt": mth}
            for var in ["const"] + model2_vars:
                slope_row[var] = result.params.get(var, np.nan)
            monthly_slopes.append(slope_row)

        slopes_df = pd.DataFrame(monthly_slopes).set_index("mthcaldt").sort_index()
        # Calculate 10-year rolling means (120 months)
        slopes_rolling = slopes_df.rolling(window=120, min_periods=60).mean()
        slopes_dict[subset_name] = slopes_rolling

    # Create the figure with two vertically stacked subplots (2 rows, 1 column)
    fig, axes = plt.subplots(nrows=2, ncols=1, figsize=(14, 10), sharex=True)
    ax_a, ax_b = axes

    # Panel A: All Stocks
    if "All stocks" in slopes_dict:
        df_a = slopes_dict["All stocks"]
        for var in model2_vars:
            ax_a.plot(df_a.index, df_a[var], label=var_labels.get(var, var))
        ax_a.set_title("Panel A: All Stocks (10-Year Rolling Slopes)")
        ax_a.set_ylabel("Slope Coefficient")
        ax_a.legend()
        # Set margins to zero so the line starts flush with the y-axis
        ax_a.margins(x=0)

    # Panel B: Large Stocks
    if "Large stocks" in slopes_dict:
        df_b = slopes_dict["Large stocks"]
        for var in model2_vars:
            ax_b.plot(df_b.index, df_b[var], label=var_labels.get(var, var))
        ax_b.set_title("Panel B: Large Stocks (10-Year Rolling Slopes)")
        ax_b.set_xlabel("Month")
        ax_b.set_ylabel("Slope Coefficient")
        ax_b.legend()
        # Set margins to zero so the line starts flush with the y-axis
        ax_b.margins(x=0)

    plt.tight_layout()
    return fig, axes

def save_data(table_1, table_2, figure_1):
    import os
    from pathlib import Path
    
    # Create output directory if it doesn't exist
    output_dir = Path('../_output')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save tables as Pickle
    table_1.to_pickle(output_dir / 'table_1.pkl')
    table_2.to_pickle(output_dir / 'table_2.pkl')

    # Save tables as LaTeX
    latex_table_1 = table_1.to_latex(index=True, bold_rows=True, multicolumn=True)
    with open(output_dir / 'table_1.tex', 'w') as f:
        f.write(latex_table_1)

    latex_table_2 = table_2.to_latex(index=True, bold_rows=True, multicolumn=True)
    with open(output_dir / 'table_2.tex', 'w') as f:
        f.write(latex_table_2)

    # Export Figure 1
    figure_1[0].savefig(output_dir / 'figure_1.pdf', bbox_inches='tight')
    
    # Create a marker file indicating successful save
    marker_file = output_dir / 'data_saved.marker'
    with open(marker_file, 'w') as f:
        from datetime import datetime
        f.write(f"Data saved successfully at {datetime.now().isoformat()}")
    
    print(f"All data saved successfully. Marker file created at {marker_file}")
    
    return marker_file

def check_if_data_saved():
    from pathlib import Path
    marker_file = Path('../_output/data_saved.marker')
    
    if marker_file.exists():
        print("Data has been saved previously.")
        with open(marker_file, 'r') as f:
            timestamp = f.read()
        print(f"Save timestamp: {timestamp}")
        return True
    else:
        print("Data has not been saved yet.")
        return False
    
def create_latex_document_from_pkl():
    """
    Reads table_1.pkl and table_2.pkl from ../_output,
    generates LaTeX tables via pandas' to_latex(),
    and writes a complete LaTeX document (research_report.tex).
    
    Advantages:
      - Avoids post-processing raw .tex files
      - Minimizes bracket / runaway-argument errors
      - Auto-escapes underscores, carets, etc.
      - Simple to tweak column names or apply further styling
    """
    import pandas as pd
    from pathlib import Path
    from datetime import datetime

    # ----------------------
    #  PATHS & CHECKS
    # ----------------------
    output_dir = Path('../_output')
    output_dir.mkdir(parents=True, exist_ok=True)

    table1_pkl = output_dir / 'table_1.pkl'
    table2_pkl = output_dir / 'table_2.pkl'
    figure1_path = output_dir / 'figure_1.pdf'

    missing_files = []
    for f in [table1_pkl, table2_pkl, figure1_path]:
        if not f.exists():
            missing_files.append(str(f))

    if missing_files:
        print("Missing files:", ", ".join(missing_files))
        return None

    # ----------------------
    #  READ THE PICKLED DFS
    # ----------------------
    try:
        df1 = pd.read_pickle(table1_pkl)
        df2 = pd.read_pickle(table2_pkl)
    except Exception as e:
        print(f"Error loading pickles: {e}")
        return None

    # ----------------------
    #  OPTIONAL CLEANUP: R^2, etc.
    # ----------------------
    # If you want to rename columns that literally contain R^2 -> $R^2$, do so here:
    # e.g. df1.columns = [col.replace("R^2", "$R^2$") for col in df1.columns]

    # Similarly, if underscores in columns are meant to be literal underscores,
    # pandas with escape=True will turn them into \_ automatically.

    # ----------------------
    #  GENERATE LATEX TABLES
    # ----------------------
    # By default, pandas will do basic tabular formatting, auto-escape underscores, etc.
    # If your data is wide, adjust the column format or other to_latex options as needed.
    # e.g. to_latex(index=False, float_format="%.3f") or to_latex(longtable=True)
    
    # Example: no index, and limit floating decimals to 3
    latex_table1 = df1.to_latex(index=False, float_format="%.4f", escape=True)
    latex_table2 = df2.to_latex(index=False, float_format="%.4f", escape=True)

    # If the DataFrame has multi-level columns or indices, you may need different arguments,
    # or you might want to flatten them first.

    # ----------------------
    #  WRAP TABLE ENVIRONMENTS
    # ----------------------
    # In many cases, you might just keep the raw output of to_latex(), but let's wrap
    # them in table floats for the final doc:

    table1_content = f"""    
\\begin{{table}}
\\centering
\\caption{{Summary Statistics}}
\\label{{tab:table1}}
{latex_table1}
\\end{{table}}"""

    table2_content = f"""\\begin{{table}}
\\centering
\\caption{{Return Predictability}}
\\label{{tab:table2}}
{latex_table2}
\\end{{table}}"""

    # ----------------------
    #  CREATE THE FULL LATEX DOC
    # ----------------------
    latex_doc = f"""\\documentclass[12pt]{{article}}
\\usepackage{{booktabs}}
\\usepackage{{graphicx}}
\\usepackage{{caption}}
\\usepackage{{geometry}}
\\usepackage{{multirow}}  % in case you need multirow in any table
\\usepackage{{placeins}}   % <-- This package adds \FloatBarrier
\\geometry{{margin=1in}}

\\title{{Return Prediction Results}}
\\author{{Financial Markets Analysis}}
\\date{{{datetime.now().strftime('%B %d, %Y')}}}

\\begin{{document}}

\\maketitle

\\section{{Data Summary}}

{table1_content}

\\clearpage
\\section{{Regression Results}}

{table2_content}

\\clearpage
\\section{{Time-Series Patterns}}
\\FloatBarrier   % <--- Force all floats before this point to appear

\\begin{{figure}}
\\caption{{Time-series of return predictability.}}
\\centering
\\includegraphics[width=0.9\\textwidth]{{{figure1_path.name}}}
\\label{{fig:figure1}}
\\end{{figure}}

\\end{{document}}
"""

    # ----------------------
    #  WRITE OUT RESEARCH_REPORT.TEX
    # ----------------------
    output_file = output_dir / "research_report.tex"
    try:
        with open(output_file, "w", encoding="utf-8") as out_f:
            out_f.write(latex_doc)
    except Exception as e:
        print(f"Error writing LaTeX document: {e}")
        return None

    print(f"LaTeX document created successfully:\n{output_file}")
    return output_file

def compile_latex_document(tex_file_path=None):
    """
    Compiles a LaTeX document to PDF using pdflatex.
    
    Parameters:
    
        tex_file_path: Path to the .tex file. If None, defaults to '../_output/research_report.tex'
    
    Returns:
        Path to the output PDF if compilation was successful, None otherwise
    """
    import subprocess
    import os
    from pathlib import Path
    import shutil
    
    # First check if pdflatex is available
    pdflatex_path = shutil.which('pdflatex')
    if not pdflatex_path:
        print("Error: pdflatex not found in PATH. Please install a LaTeX distribution like MiKTeX or TeXLive.")
        print("Alternatively, you can compile the .tex file manually.")
        return None
    
    # Default path if none provided
    if tex_file_path is None:
        tex_file_path = Path('../_output/research_report.tex')
    else:
        tex_file_path = Path(tex_file_path)
    
    if not tex_file_path.exists():
        print(f"Error: LaTeX file not found at {tex_file_path}")
        return None
        
    # Get directory and filename
    tex_dir = tex_file_path.parent
    tex_filename = tex_file_path.name
    
    # Change to the directory containing the .tex file
    original_dir = os.getcwd()
    os.chdir(tex_dir)
    
    try:
        print(f"Compiling {tex_filename} with pdflatex...")
        # Run pdflatex twice to resolve references
        for i in range(2):
            print(f"LaTeX compilation pass {i+1}...")
            # Use -interaction=nonstopmode to continue on errors
            result = subprocess.run(
                [pdflatex_path, '-interaction=nonstopmode', tex_filename],
                capture_output=True, 
                text=True
            )
            
            if result.returncode != 0:
                print("LaTeX compilation errors:")
                print(result.stderr)
                # Don't exit yet, sometimes it works despite errors
        
        # Check if PDF was created
        pdf_path = tex_file_path.with_suffix('.pdf')
        
        if pdf_path.exists():
            print(f"PDF successfully compiled: {pdf_path}")
            # Return to original directory
            os.chdir(original_dir)
            return pdf_path
        else:
            print(f"PDF compilation failed. No file found at {pdf_path}")
            os.chdir(original_dir)
            return None
            
    except Exception as e:
        print(f"Error during compilation: {str(e)}")
        os.chdir(original_dir)
        return None
        
    finally:
        # Make sure we return to original directory even if there's an error
        os.chdir(original_dir)

if __name__ == "__main__":

    # 1) Load raw data
    comp = load_cache_data(data_dir=RAW_DATA_DIR, file_name="Compustat_fund.parquet")
    ccm  = load_cache_data(data_dir=RAW_DATA_DIR, file_name="CRSP_Comp_Link_Table.parquet")
    crsp_d = load_cache_data(data_dir=RAW_DATA_DIR, file_name="CRSP_stock_d.parquet")
    crsp_m = load_cache_data(data_dir=RAW_DATA_DIR, file_name="CRSP_stock_m.parquet")
    crsp_index_d = load_cache_data(data_dir=RAW_DATA_DIR, file_name="CRSP_index_d.parquet")

    # 2) Calculate market equity
    crsp = calculate_market_equity(crsp_m)

    # 2) Add report date and calculate book equity
    comp = add_report_date(comp)
    comp = calc_book_equity(comp)
    comp = expand_compustat_annual_to_monthly(comp)

    # 3) Merge comp + crsp_m + ccm => crsp_comp
    crsp_comp = merge_CRSP_and_Compustat(crsp, comp, ccm)

    # 4 Calculate factors
    crsp_comp, factors_dict = get_factors(crsp_comp, crsp_d, crsp_index_d)

    # 6) Create subsets for analysis
    subsets_comp_crsp,  = get_subsets(crsp_comp) # Dictionary of dataframes corresponding of the data sets

    # 7) Build Table 1
    table_1 = build_table_1(subsets_comp_crsp)

    # 8) Build Table 2
    table_2 = build_table_2(subsets_comp_crsp, factors_dict)
    
    # 9) Create Figure 1
    figure_1 = create_figure_1(subsets_comp_crsp)

    # 10) Save data
    save_data(table_1, table_2, figure_1)
    
    # 11) Create and compile LaTeX document
    create_latex_document_from_pkl()
    compile_latex_document()