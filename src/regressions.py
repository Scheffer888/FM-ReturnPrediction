import pandas as pd
import numpy as np
import statsmodels.api as sm

# ================================================================================================
# Regressions
# ================================================================================================

def run_monthly_cs_regressions(
    df: pd.DataFrame, 
    return_col: str,
    predictor_cols: list,
    date_col: str = "mthcaldt"
) -> pd.DataFrame:
    """
    Runs cross-sectional regressions each month:
       return_col(t) = alpha_t + b_1(t)*X_1(t-1) + ... + e_i,t
    and returns a DataFrame with columns: ['date','slope_X1','slope_X2',...,'R2','N'].

    Parameters
    ----------
    df : pd.DataFrame
        Must have at least [date_col, return_col] + predictor_cols.
        Each row = firm-month observation. 
    return_col : str
        Name of the column containing the monthly return. 
    predictor_cols : list of str
        Names of the lagged firm characteristics used as regressors.
    date_col : str
        Name of the column with the month identifiers (e.g. "1964-05-31").

    Returns
    -------
    pd.DataFrame
        One row per month, with columns:
          date_col, slope_for_each_predictor, R2, N
    """
    # Sort by date to ensure groupby processes in chronological order
    df = df[[return_col, date_col] +  predictor_cols].sort_values(date_col).dropna()  # drop rows missing the dep.var

    results_list = []

    for g_date, grp in df.groupby(date_col):
        # The cross section for month == g_date
        # Y is that month's returns (in percent, presumably)
        Y = grp[return_col].values

        # X is your predictor matrix.  Typically we include an intercept.
        X = grp[predictor_cols].values
        X = sm.add_constant(X, prepend=True)  # Intercept in first column

        if len(grp) < len(predictor_cols) + 1:
            # Too few stocks to estimate the cross-sectional regression
            continue

        # Regress: ret_i,t on X_i,t
        mod = sm.OLS(Y, X).fit()

        # Slopes (the first param is intercept)
        slopes = mod.params[1:]  # skip intercept
        # Cross-sectional R^2: 1 - SSE/SST
        # SSE = sum of residuals^2,  SST = sum((Y - mean(Y))^2)
        # But statsmodels “mod.rsquared” is the usual measure, so we can just use that:
        r2_cs = mod.rsquared
        n_stocks = len(grp)

        # Build one row
        row_dict = {date_col: g_date, 'N': n_stocks, 'R2': r2_cs}
        # fill in slope_i for each predictor
        for i, col in enumerate(predictor_cols):
            row_dict[f"slope_{col}"] = slopes[i]
        results_list.append(row_dict)

    # Combine into a DataFrame
    results_df = pd.DataFrame(results_list)
    return results_df

def newey_west_mean_se(slopes: np.ndarray, lags: int = 4) -> float:
    """
    Compute the Newey-West standard error for the mean of a univariate
    time series `slopes`, allowing for serial correlation up to `lags`.
    """
    x = np.asarray(slopes, dtype=float)
    T = x.size
    if T < 2:
        return np.nan
    mean_x = x.mean()
    u = x - mean_x

    gamma0 = np.sum(u * u)
    sum_covar = 0.0
    for k in range(1, lags + 1):
        gamma_k = np.sum(u[k:] * u[:-k])
        weight = 1.0 - (k / T)
        if weight < 0:
            break
        sum_covar += weight * gamma_k

    var_mean = (gamma0 + 2.0 * sum_covar) / (T**2)
    return np.sqrt(var_mean)

def fama_macbeth_summary(cs_results: pd.DataFrame,
                         predictor_cols: list,
                         date_col="mthcaldt",
                         nw_lags=4) -> pd.Series:
    """
    Summarize monthly cross-sectional regression results with
    Fama–MacBeth method + Newey-West standard errors for the average slope.
    """
    out = {}
    for col in predictor_cols:
        slope_col = f"slope_{col}"
        slopes_ts = cs_results[slope_col].dropna()
        if len(slopes_ts) < 10:
            out[f"{col}_coef"]  = np.nan
            out[f"{col}_tstat"] = np.nan
            continue

        # average slope
        mean_slope = slopes_ts.mean()
        out[f"{col}_coef"] = mean_slope

        # NW standard error for the average
        slope_se = newey_west_mean_se(slopes_ts, lags=nw_lags)
        t_stat = mean_slope / slope_se
        out[f"{col}_tstat"] = t_stat

    out["mean_R2"] = cs_results["R2"].mean()
    out["mean_N"]  = cs_results["N"].mean()

    return pd.Series(out)