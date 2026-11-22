"""
Audit Model Upgrades:
1) Combined 2-Sleeve Portfolio Test (Refiners Short Q5 vs XLE + Steel Long Q5 vs XME):
   - Sleeve-to-sleeve return correlation across 10-day steps.
   - Combined dual-sleeve books (50/50 capital allocation and inverse-volatility weighted).
   - Honest median per-book circular block-bootstrap p-values across 10 staggered books.
2) Conviction Scaling vs. Binary Q80 (Both Sleeves):
   - Flat Equal-Weight Q80 vs Linear Conviction Weighting above Q80 (causal q80 & q95).
   - Persistence Filter (3-day rolling mean >= Q80 and 2-of-3 clear-sky days).
3) Holding Horizon & Alpha Decay Sweep (5d, 10d, 15d, 20d):
   - Non-overlapping staggered books at H = 5, 10, 15, 20 trading days.
   - Sharpe, Net Return, Profit Factor, Win Rate, and Median Per-Book Bootstrap p-value.

Strictly isolated research script: does NOT modify any live execution files.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_model_upgrades.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import pearsonr, spearmanr

from src.eval.audit_industrial import (
    BETA_WINDOW,
    CLOUD_MAX,
    FACILITIES_PATH,
    build_industrial_features,
)
from src.eval.audit_short_q5 import (
    EQ_WT_LEG,
    INITIAL_EQUITY,
    MAX_GROSS_WT,
    build_panel as build_refiner_panel,
)

CRACK_PATH = Path("data/silver/crack_spread.parquet")
BRONZE_IND_PATH = Path("data/industrial/bronze_industrial.parquet")

COST_RT = 0.0025            # 25 bps round-trip execution & borrow cost
BOOT_DRAWS = 10_000
HORIZONS = (5, 10, 15, 20)

REFINER_TICKERS = ["VLO", "MPC", "PSX", "DINO", "PBF", "DK", "CVI"]
STEEL_TICKERS = ["CLF", "CMC", "NUE", "STLD"]


def block_bootstrap_stats(step_rets: np.ndarray, holding_days: int, n_draws: int = BOOT_DRAWS, seed: int = 42) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    clean = step_rets[~np.isnan(step_rets)]
    n = len(clean)
    if n < 5:
        return {"p_val": np.nan, "ci_lo_pct": np.nan, "ci_hi_pct": np.nan, "boot_sh_med": np.nan}

    block_len = max(2, int(round(np.sqrt(n))))
    n_blocks = int(np.ceil(n / block_len))
    boot_sharpes = np.empty(n_draws, dtype=float)
    boot_means = np.empty(n_draws, dtype=float)
    ann_factor = np.sqrt(252.0 / holding_days)

    for b in range(n_draws):
        starts = rng.integers(0, n, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) % n for s in starts])[:n]
        sample = clean[idx]
        sd = np.std(sample, ddof=1)
        boot_sharpes[b] = (np.mean(sample) / sd * ann_factor) if sd > 0 else 0.0
        boot_means[b] = np.mean(sample) * 100.0

    p_val = float(np.mean(boot_sharpes <= 0.0))
    lo, hi = np.percentile(boot_means, [2.5, 97.5])
    return {
        "p_val": p_val,
        "ci_lo_pct": float(lo),
        "ci_hi_pct": float(hi),
        "boot_sh_med": float(np.median(boot_sharpes)),
    }


def eval_bootstrap_summary(book_step_rets: list[np.ndarray], holding_days: int) -> dict[str, float]:
    p_vals, lo_vals, hi_vals, med_sharpes = [], [], [], []
    for b_arr in book_step_rets:
        stats = block_bootstrap_stats(b_arr, holding_days=holding_days)
        if not np.isnan(stats["p_val"]):
            p_vals.append(stats["p_val"])
            lo_vals.append(stats["ci_lo_pct"])
            hi_vals.append(stats["ci_hi_pct"])
            med_sharpes.append(stats["boot_sh_med"])
    return {
        "med_p_val": float(np.median(p_vals)) if p_vals else np.nan,
        "med_ci_lo": float(np.median(lo_vals)) if lo_vals else np.nan,
        "med_ci_hi": float(np.median(hi_vals)) if hi_vals else np.nan,
        "med_boot_sh": float(np.median(med_sharpes)) if med_sharpes else np.nan,
    }


def load_all_market_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetches full market prices and returns forward excess returns across all horizons."""
    all_symbols = REFINER_TICKERS + ["XLE"] + STEEL_TICKERS + ["XME"]
    raw = yf.download(all_symbols, start="2017-06-01", auto_adjust=False, progress=False)
    field = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    rets = px.pct_change()

    # Refiner excess returns
    ref_rows = []
    xle_px = px["XLE"]
    xle_ret = rets["XLE"]
    for tkr in REFINER_TICKERS:
        beta = rets[tkr].rolling(BETA_WINDOW, min_periods=30).cov(xle_ret) / xle_ret.rolling(
            BETA_WINDOW, min_periods=30
        ).var().replace(0.0, np.nan)
        tdf = pd.DataFrame({"trading_date": px.index, "ticker": tkr, "adj_close": px[tkr].values, "beta_60d": beta.values})
        for h in HORIZONS:
            fwd_s = px[tkr].shift(-h) / px[tkr] - 1.0
            fwd_e = xle_px.shift(-h) / xle_px - 1.0
            tdf[f"fwd_ret_{h}d_excess"] = (fwd_s - beta * fwd_e).values
        ref_rows.append(tdf)
    ref_mkt = pd.concat(ref_rows, ignore_index=True)

    # Steel excess returns & XME momentum
    steel_rows = []
    xme_px = px["XME"]
    xme_ret = rets["XME"]
    xme_mom20 = (xme_px / xme_px.shift(20) - 1.0).values
    for tkr in STEEL_TICKERS:
        beta = rets[tkr].rolling(BETA_WINDOW, min_periods=30).cov(xme_ret) / xme_ret.rolling(
            BETA_WINDOW, min_periods=30
        ).var().replace(0.0, np.nan)
        tdf = pd.DataFrame(
            {
                "trading_date": px.index,
                "ticker": tkr,
                "adj_close": px[tkr].values,
                "beta_60d": beta.values,
                "xme_mom20": xme_mom20,
            }
        )
        for h in HORIZONS:
            fwd_s = px[tkr].shift(-h) / px[tkr] - 1.0
            fwd_e = xme_px.shift(-h) / xme_px - 1.0
            tdf[f"fwd_ret_{h}d_excess"] = (fwd_s - beta * fwd_e).values
        steel_rows.append(tdf)
    steel_mkt = pd.concat(steel_rows, ignore_index=True)

    return ref_mkt, steel_mkt


def load_datasets() -> tuple[pd.DataFrame, pd.DataFrame, list[pd.Timestamp]]:
    """Loads feature panels for Refiners and Steel, merging prices and macro gates."""
    ref_mkt, steel_mkt = load_all_market_data()

    # 1. Refiners Panel
    r_base = build_refiner_panel()
    crack = pd.read_parquet(CRACK_PATH)[["trading_date", "crack_z60"]].sort_values("trading_date")
    r_df = pd.merge_asof(r_base.sort_values("trading_date"), crack, on="trading_date", direction="backward")
    r_df["crack_ok"] = r_df["crack_z60"] <= 1.0

    # Drop existing forward returns in r_df to use multi-horizon ones
    cols_to_drop = [c for c in r_df.columns if c.startswith("fwd_ret_")]
    r_df = r_df.drop(columns=cols_to_drop).merge(ref_mkt, on=["ticker", "trading_date"], how="inner")

    # Add persistence indicators for refiners
    r_frames = []
    for _, g in r_df.groupby("ticker"):
        g = g.sort_values("trading_date").copy()
        g["yoy_3d_mean"] = g["yoy_per_pass"].rolling(3, min_periods=1).mean()
        r_frames.append(g)
    r_df = pd.concat(r_frames, ignore_index=True)

    # 2. Steel Panel
    fac_gdf = gpd.read_file(FACILITIES_PATH)
    steel_gdf = fac_gdf[fac_gdf["sector"] == "STEEL"].copy()
    bronze = pd.read_parquet(BRONZE_IND_PATH)
    s_gold = build_industrial_features(bronze, steel_gdf)
    s_df = s_gold.merge(steel_mkt, on=["ticker", "trading_date"], how="inner")
    s_df["xme_mom20_pos"] = s_df["xme_mom20"] > 0.0

    # Add persistence indicators for steel
    s_frames = []
    for _, g in s_df.groupby("ticker"):
        g = g.sort_values("trading_date").copy()
        g["yoy_3d_mean"] = g["yoy_per_pass"].rolling(3, min_periods=1).mean()
        s_frames.append(g)
    s_df = pd.concat(s_frames, ignore_index=True)

    # Common dates
    r_dates = set(r_df["trading_date"].unique())
    s_dates = set(s_df["trading_date"].unique())
    common_dates = sorted(r_dates.intersection(s_dates))

    r_df = r_df[r_df["trading_date"].isin(set(common_dates))].sort_values(["trading_date", "ticker"]).reset_index(drop=True)
    s_df = s_df[s_df["trading_date"].isin(set(common_dates))].sort_values(["trading_date", "ticker"]).reset_index(drop=True)

    return r_df, s_df, common_dates


# ==============================================================================
# Simulation Core
# ==============================================================================

def run_sleeve_simulation(
    df: pd.DataFrame,
    side: str,  # "SHORT" (refiners) or "LONG" (steel)
    holding_days: int,
    signal_col: str = "yoy_per_pass",
    weighting_mode: str = "flat",  # "flat", "conviction", "conviction_v2"
    persistence_mode: str | None = None,  # None, "3d_mean", "2of3_clear"
    extra_mask_col: str | None = None,
    all_dates: list[pd.Timestamp] | None = None,
    cost_rt: float = COST_RT,
    *,
    trade_records: list[dict] | None = None,
) -> tuple[pd.DataFrame, list[np.ndarray], list[list[float]]]:
    """Runs staggered non-overlapping books and returns book stats, step returns, and trade PnLs."""
    if all_dates is None:
        all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    fwd_col = f"fwd_ret_{holding_days}d_excess"
    sign = -1.0 if side == "SHORT" else 1.0

    q_cache: dict[pd.Timestamp, tuple[float, float] | None] = {}

    def get_cutoffs(dt: pd.Timestamp) -> tuple[float, float] | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < holding_days:
            q_cache[dt] = None
            return None
        embargo_dt = all_dates[idx - holding_days]
        hist = df[
            (df["trading_date"] <= embargo_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df[signal_col].notna())
            & (df[fwd_col].notna())
        ]
        if len(hist) < 40:
            q_cache[dt] = None
            return None
        q80 = float(np.quantile(hist[signal_col].values, 0.80))
        q95 = float(np.quantile(hist[signal_col].values, 0.95))
        q_cache[dt] = (q80, q95)
        return q_cache[dt]

    book_rows = []
    book_step_rets = []
    book_trade_pnls = []

    for offset in range(holding_days):
        eq, peak, dd = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        trade_pnls: list[float] = []
        step_rets: list[float] = []

        for dt in all_dates[offset::holding_days]:
            idx = date_to_idx[dt]
            if idx + holding_days >= len(all_dates):
                continue
            cuts = get_cutoffs(dt)
            if cuts is None:
                step_rets.append(0.0)
                continue
            q80, q95 = cuts

            # Candidate filter
            cond = (
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df[signal_col] >= q80)
                & (df[fwd_col].notna())
            )
            if "regime_label" in df.columns:
                cond &= df["regime_label"] == "IDIOSYNCRATIC"
            if extra_mask_col is not None:
                cond &= df[extra_mask_col].fillna(False)

            if persistence_mode == "3d_mean":
                cond &= df["yoy_3d_mean"] >= q80

            cands = df[cond].copy()
            if cands.empty:
                step_rets.append(0.0)
                continue

            # Weighting
            if weighting_mode == "flat":
                raw_w = np.full(len(cands), EQ_WT_LEG)
            elif weighting_mode == "conviction":
                # Prompt formula: clip((yoy - q80)/(q95 - q80), 0.5, 1.5)
                denom = max(1e-6, q95 - q80)
                mult = np.clip((cands[signal_col].values - q80) / denom, 0.5, 1.5)
                raw_w = EQ_WT_LEG * mult
            elif weighting_mode == "conviction_v2":
                # Linear 0.5 at q80 to 1.5 at q95
                denom = max(1e-6, q95 - q80)
                mult = np.clip(0.5 + (cands[signal_col].values - q80) / denom, 0.5, 1.5)
                raw_w = EQ_WT_LEG * mult
            else:
                raw_w = np.full(len(cands), EQ_WT_LEG)

            sc = min(1.0, MAX_GROSS_WT / max(1e-6, np.sum(raw_w)))
            final_w = raw_w * sc

            step_pnl = 0.0
            for w_i, (_, r) in zip(final_w, cands.iterrows()):
                trade_net_ret = sign * float(r[fwd_col]) - cost_rt
                pnl = eq * w_i * trade_net_ret
                step_pnl += pnl
                trade_pnls.append(pnl)
                if trade_records is not None:
                    trade_records.append({
                        "offset": offset,
                        "trading_date": dt,
                        "ticker": r["ticker"],
                        "side": side,
                        "weight": w_i,
                        "net_ret": trade_net_ret,
                        "net_bps": trade_net_ret * 10_000,
                    })

            step_rets.append(step_pnl / eq)
            eq += step_pnl
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)

        gp = sum(p for p in trade_pnls if p > 0)
        gl = abs(sum(p for p in trade_pnls if p < 0))
        pf = gp / gl if gl > 0 else np.nan
        b_arr = np.array(step_rets, dtype=float)
        ann_factor = np.sqrt(252.0 / holding_days)
        b_sh = (float(np.mean(b_arr) / np.std(b_arr, ddof=1) * ann_factor)) if len(b_arr) > 1 and np.std(b_arr, ddof=1) > 0 else 0.0
        n_win = sum(1 for p in trade_pnls if p > 0)
        wr = (n_win / len(trade_pnls)) * 100.0 if trade_pnls else 0.0

        book_rows.append(
            {
                "offset": offset,
                "n": len(trade_pnls),
                "net_pct": (eq / INITIAL_EQUITY - 1.0) * 100.0,
                "sh": b_sh,
                "pf": pf,
                "dd_pct": dd * 100.0,
                "wr": wr,
            }
        )
        book_step_rets.append(b_arr)
        book_trade_pnls.append(trade_pnls)

    res = pd.DataFrame(book_rows)
    return res, book_step_rets, book_trade_pnls


# ==============================================================================
# Part 1: Combined Dual-Sleeve Portfolio
# ==============================================================================

def run_part1_combined(r_df: pd.DataFrame, s_df: pd.DataFrame, all_dates: list[pd.Timestamp]) -> None:
    print("\n" + "=" * 90)
    print("PART 1: COMBINED DUAL-SLEEVE PORTFOLIO TEST (10-DAY BOOKS, NET 25 BPS RT)")
    print("=" * 90)

    # 1. Standalone Sleeves
    r_books, r_steps, r_trades = run_sleeve_simulation(
        r_df, side="SHORT", holding_days=10, extra_mask_col="crack_ok", all_dates=all_dates
    )
    s_books_all, s_steps_all, s_trades_all = run_sleeve_simulation(
        s_df, side="LONG", holding_days=10, extra_mask_col=None, all_dates=all_dates
    )
    s_books_mom, s_steps_mom, s_trades_mom = run_sleeve_simulation(
        s_df, side="LONG", holding_days=10, extra_mask_col="xme_mom20_pos", all_dates=all_dates
    )

    # Correlation analysis across steps
    pooled_r1 = np.concatenate(r_steps)
    pooled_s_all = np.concatenate(s_steps_all)
    pooled_s_mom = np.concatenate(s_steps_mom)

    pr_all, pp_all = pearsonr(pooled_r1, pooled_s_all)
    sr_all, sp_all = spearmanr(pooled_r1, pooled_s_all)
    pr_mom, pp_mom = pearsonr(pooled_r1, pooled_s_mom)
    sr_mom, sp_mom = spearmanr(pooled_r1, pooled_s_mom)

    print(f"\n--- Sleeve-to-Sleeve 10-Day Step Return Correlation (N={len(pooled_r1)} non-overlapping steps) ---")
    print(f"  Refiners Short vs. Steel Long (All)     : Pearson r = {pr_all:+.4f} (p={pp_all:.4f}) | Spearman rho = {sr_all:+.4f} (p={sp_all:.4f})")
    print(f"  Refiners Short vs. Steel Long (Mom Gate): Pearson r = {pr_mom:+.4f} (p={pp_mom:.4f}) | Spearman rho = {sr_mom:+.4f} (p={sp_mom:.4f})")

    # 2. Combined Portfolios
    comb_variants = [
        ("Refiners Short Q5 Standalone", r_books, r_steps),
        ("Steel Long Q5 Standalone (All)", s_books_all, s_steps_all),
        ("Steel Long Q5 Standalone (Mom Gate)", s_books_mom, s_steps_mom),
    ]

    # Combined 50/50: Refiners + Steel All
    comb_5050_all_steps = []
    comb_5050_all_rows = []
    for off in range(10):
        r1, r2 = r_steps[off], s_steps_all[off]
        rc = 0.5 * r1 + 0.5 * r2
        eq = INITIAL_EQUITY * np.prod(1.0 + rc)
        peaks = np.maximum.accumulate(INITIAL_EQUITY * np.cumprod(1.0 + rc))
        dd = np.max((peaks - INITIAL_EQUITY * np.cumprod(1.0 + rc)) / peaks) if len(rc) > 0 else 0.0
        sh = (np.mean(rc) / np.std(rc, ddof=1) * np.sqrt(25.2)) if len(rc) > 1 and np.std(rc, ddof=1) > 0 else 0.0
        pos = sum(p for p in rc if p > 0)
        neg = abs(sum(p for p in rc if p < 0))
        pf = pos / neg if neg > 0 else np.nan
        comb_5050_all_rows.append({"n": r_books.iloc[off]["n"] + s_books_all.iloc[off]["n"], "net_pct": (eq / INITIAL_EQUITY - 1.0) * 100, "sh": sh, "pf": pf, "dd_pct": dd * 100})
        comb_5050_all_steps.append(rc)
    comb_variants.append(("Combined Dual-Sleeve (50/50, Refiners + Steel All)", pd.DataFrame(comb_5050_all_rows), comb_5050_all_steps))

    # Combined 50/50: Refiners + Steel Mom Gate
    comb_5050_mom_steps = []
    comb_5050_mom_rows = []
    for off in range(10):
        r1, r2 = r_steps[off], s_steps_mom[off]
        rc = 0.5 * r1 + 0.5 * r2
        eq = INITIAL_EQUITY * np.prod(1.0 + rc)
        peaks = np.maximum.accumulate(INITIAL_EQUITY * np.cumprod(1.0 + rc))
        dd = np.max((peaks - INITIAL_EQUITY * np.cumprod(1.0 + rc)) / peaks) if len(rc) > 0 else 0.0
        sh = (np.mean(rc) / np.std(rc, ddof=1) * np.sqrt(25.2)) if len(rc) > 1 and np.std(rc, ddof=1) > 0 else 0.0
        pos = sum(p for p in rc if p > 0)
        neg = abs(sum(p for p in rc if p < 0))
        pf = pos / neg if neg > 0 else np.nan
        comb_5050_mom_rows.append({"n": r_books.iloc[off]["n"] + s_books_mom.iloc[off]["n"], "net_pct": (eq / INITIAL_EQUITY - 1.0) * 100, "sh": sh, "pf": pf, "dd_pct": dd * 100})
        comb_5050_mom_steps.append(rc)
    comb_variants.append(("Combined Dual-Sleeve (50/50, Refiners + Steel Mom Gate)", pd.DataFrame(comb_5050_mom_rows), comb_5050_mom_steps))

    # Combined Inverse-Vol: Refiners + Steel Mom Gate
    comb_inv_steps = []
    comb_inv_rows = []
    vol_r = np.std(pooled_r1, ddof=1)
    vol_s = np.std(pooled_s_mom, ddof=1)
    w_r = (1.0 / max(1e-4, vol_r)) / (1.0 / max(1e-4, vol_r) + 1.0 / max(1e-4, vol_s))
    w_s = 1.0 - w_r
    for off in range(10):
        r1, r2 = r_steps[off], s_steps_mom[off]
        rc = w_r * r1 + w_s * r2
        eq = INITIAL_EQUITY * np.prod(1.0 + rc)
        peaks = np.maximum.accumulate(INITIAL_EQUITY * np.cumprod(1.0 + rc))
        dd = np.max((peaks - INITIAL_EQUITY * np.cumprod(1.0 + rc)) / peaks) if len(rc) > 0 else 0.0
        sh = (np.mean(rc) / np.std(rc, ddof=1) * np.sqrt(25.2)) if len(rc) > 1 and np.std(rc, ddof=1) > 0 else 0.0
        pos = sum(p for p in rc if p > 0)
        neg = abs(sum(p for p in rc if p < 0))
        pf = pos / neg if neg > 0 else np.nan
        comb_inv_rows.append({"n": r_books.iloc[off]["n"] + s_books_mom.iloc[off]["n"], "net_pct": (eq / INITIAL_EQUITY - 1.0) * 100, "sh": sh, "pf": pf, "dd_pct": dd * 100})
        comb_inv_steps.append(rc)
    comb_variants.append((f"Combined Inv-Vol ({w_r:.0%}/{w_s:.0%}, Refiners + Steel Mom)", pd.DataFrame(comb_inv_rows), comb_inv_steps))

    # Combined 50/50: Conviction-Scaled Refiners + Unfiltered Flat Steel
    r_books_conv, r_steps_conv, _ = run_sleeve_simulation(
        r_df, side="SHORT", holding_days=10, weighting_mode="conviction", extra_mask_col="crack_ok", all_dates=all_dates
    )
    comb_conv_steel_steps = []
    comb_conv_steel_rows = []
    for off in range(10):
        r1, r2 = r_steps_conv[off], s_steps_all[off]
        rc = 0.5 * r1 + 0.5 * r2
        eq = INITIAL_EQUITY * np.prod(1.0 + rc)
        peaks = np.maximum.accumulate(INITIAL_EQUITY * np.cumprod(1.0 + rc))
        dd = np.max((peaks - INITIAL_EQUITY * np.cumprod(1.0 + rc)) / peaks) if len(rc) > 0 else 0.0
        sh = (np.mean(rc) / np.std(rc, ddof=1) * np.sqrt(25.2)) if len(rc) > 1 and np.std(rc, ddof=1) > 0 else 0.0
        pos = sum(p for p in rc if p > 0)
        neg = abs(sum(p for p in rc if p < 0))
        pf = pos / neg if neg > 0 else np.nan
        comb_conv_steel_rows.append({"n": r_books_conv.iloc[off]["n"] + s_books_all.iloc[off]["n"], "net_pct": (eq / INITIAL_EQUITY - 1.0) * 100, "sh": sh, "pf": pf, "dd_pct": dd * 100})
        comb_conv_steel_steps.append(rc)
    comb_variants.append(("★ Production Target: Conviction Refiners + Flat Steel (50/50)", pd.DataFrame(comb_conv_steel_rows), comb_conv_steel_steps))


    print(f"\nInverse-Vol Weighting: Refiners Weight = {w_r:.1%}, Steel Weight = {w_s:.1%}")
    print("\n--- COMBINED 2-SLEEVE PORTFOLIO PERFORMANCE TABLE ---")
    header = f"{'Configuration':54s} | {'Avg N':6s} | {'Net Ret':8s} | {'Sharpe':7s} | {'PF':5s} | {'Max DD':6s} | {'Pos':5s} | {'MedBootP':8s} | {'95% CI'}"
    print(header)
    print("-" * len(header))

    for name, bdf, steps in comb_variants:
        boot = eval_bootstrap_summary(steps, holding_days=10)
        pos_cnt = int((bdf["net_pct"] > 0).sum())
        print(
            f"{name:54s} | {bdf['n'].mean():6.1f} | {bdf['net_pct'].mean():+7.2f}% | "
            f"{bdf['sh'].mean():+6.2f}  | {bdf['pf'].mean():5.2f} | {bdf['dd_pct'].mean():5.2f}% | "
            f"{pos_cnt:2d}/10 | {boot['med_p_val']:8.3f} | [{boot['med_ci_lo']:+.2f}%, {boot['med_ci_hi']:+.2f}%]"
        )


# ==============================================================================
# Part 2: Conviction Scaling vs. Binary Q80
# ==============================================================================

def run_part2_conviction(r_df: pd.DataFrame, s_df: pd.DataFrame, all_dates: list[pd.Timestamp]) -> None:
    print("\n" + "=" * 90)
    print("PART 2: CONVICTION SCALING & PERSISTENCE FILTER AUDIT (10-DAY BOOKS, NET 25 BPS RT)")
    print("=" * 90)

    experiments = [
        # (Sleeve label, df, side, extra_mask, weighting_mode, persistence_mode)
        ("Refiners Short: Flat Binary Q80 (Baseline)", r_df, "SHORT", "crack_ok", "flat", None),
        ("Refiners Short: Linear Conviction [0.5, 1.5]", r_df, "SHORT", "crack_ok", "conviction", None),
        ("Refiners Short: Linear Conviction V2 [0.5, 1.5]", r_df, "SHORT", "crack_ok", "conviction_v2", None),
        ("Refiners Short: Persistence (3d YoY Mean >= Q80)", r_df, "SHORT", "crack_ok", "flat", "3d_mean"),
        ("Refiners Short: Conviction + Persistence", r_df, "SHORT", "crack_ok", "conviction", "3d_mean"),
        ("Steel Long (Mom): Flat Binary Q80 (Baseline)", s_df, "LONG", "xme_mom20_pos", "flat", None),
        ("Steel Long (Mom): Linear Conviction [0.5, 1.5]", s_df, "LONG", "xme_mom20_pos", "conviction", None),
        ("Steel Long (Mom): Linear Conviction V2 [0.5, 1.5]", s_df, "LONG", "xme_mom20_pos", "conviction_v2", None),
        ("Steel Long (Mom): Persistence (3d YoY Mean >= Q80)", s_df, "LONG", "xme_mom20_pos", "flat", "3d_mean"),
        ("Steel Long (Mom): Conviction + Persistence", s_df, "LONG", "xme_mom20_pos", "conviction", "3d_mean"),
    ]

    header = f"{'Experiment':52s} | {'Avg N':6s} | {'Net Ret':8s} | {'Sharpe':7s} | {'PF':5s} | {'Max DD':6s} | {'Pos':5s} | {'MedBootP':8s} | {'95% CI'}"
    print("\n" + header)
    print("-" * len(header))

    for name, df, side, mask, w_mode, p_mode in experiments:
        bdf, steps, _ = run_sleeve_simulation(
            df,
            side=side,
            holding_days=10,
            weighting_mode=w_mode,
            persistence_mode=p_mode,
            extra_mask_col=mask,
            all_dates=all_dates,
        )
        boot = eval_bootstrap_summary(steps, holding_days=10)
        pos_cnt = int((bdf["net_pct"] > 0).sum())
        print(
            f"{name:52s} | {bdf['n'].mean():6.1f} | {bdf['net_pct'].mean():+7.2f}% | "
            f"{bdf['sh'].mean():+6.2f}  | {bdf['pf'].mean():5.2f} | {bdf['dd_pct'].mean():5.2f}% | "
            f"{pos_cnt:2d}/10 | {boot['med_p_val']:8.3f} | [{boot['med_ci_lo']:+.2f}%, {boot['med_ci_hi']:+.2f}%]"
        )


# ==============================================================================
# Part 3: Holding Horizon & Alpha Decay Sweep
# ==============================================================================

def run_part3_horizons(r_df: pd.DataFrame, s_df: pd.DataFrame, all_dates: list[pd.Timestamp]) -> None:
    print("\n" + "=" * 90)
    print("PART 3: HOLDING HORIZON & ALPHA DECAY SWEEP (5d, 10d, 15d, 20d, NET 25 BPS RT)")
    print("=" * 90)

    header = f"{'Sleeve':28s} | {'H (Days)':8s} | {'Avg N':6s} | {'Net Ret':8s} | {'Sharpe':7s} | {'PF':5s} | {'Win Rate':8s} | {'Pos Books':9s} | {'MedBootP':8s}"
    print("\n" + header)
    print("-" * len(header))

    for sleeve_label, df, side, mask in [
        ("Refiners Short Q5", r_df, "SHORT", "crack_ok"),
        ("Steel Long Q5 (Mom Gate)", s_df, "LONG", "xme_mom20_pos"),
    ]:
        for h in HORIZONS:
            bdf, steps, _ = run_sleeve_simulation(
                df,
                side=side,
                holding_days=h,
                extra_mask_col=mask,
                all_dates=all_dates,
            )
            boot = eval_bootstrap_summary(steps, holding_days=h)
            pos_cnt = int((bdf["net_pct"] > 0).sum())
            total_b = len(bdf)
            print(
                f"{sleeve_label:28s} | {h:2d} days   | {bdf['n'].mean():6.1f} | {bdf['net_pct'].mean():+7.2f}% | "
                f"{bdf['sh'].mean():+6.2f}  | {bdf['pf'].mean():5.2f} | {bdf['wr'].mean():7.1f}% | "
                f"{pos_cnt:2d}/{total_b:2d}     | {boot['med_p_val']:8.3f}"
            )
        print("-" * len(header))


def main() -> None:
    print("[audit] Loading datasets and panel features...")
    r_df, s_df, all_dates = load_datasets()
    print(f"[audit] Loaded {len(r_df):,} Refiner rows and {len(s_df):,} Steel rows across {len(all_dates):,} common trading dates.")

    run_part1_combined(r_df, s_df, all_dates)
    run_part2_conviction(r_df, s_df, all_dates)
    run_part3_horizons(r_df, s_df, all_dates)


if __name__ == "__main__":
    main()
