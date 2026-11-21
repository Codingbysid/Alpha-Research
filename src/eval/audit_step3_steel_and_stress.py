"""
Step 3 Audit:
1) Long Q5 US Steel (CLF, NUE, STLD vs XME) with optional 20d XME momentum gate
   across 10 staggered non-overlapping 10-day books (net of 25 bps RT).
2) Circular block-bootstrap (10,000 draws) on the 10-book return series.
3) Round-trip execution/borrow cost sensitivity sweep (15, 25, 50, 75 bps).

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_step3_steel_and_stress.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import spearmanr

from src.eval.audit_industrial import (
    BETA_WINDOW,
    CLOUD_MAX,
    FACILITIES_PATH,
    HOLDING_DAYS,
    build_industrial_features,
    ingest_industrial_bronze,
)
from src.eval.audit_short_q5 import (
    EQ_WT_LEG,
    INITIAL_EQUITY,
    MAX_GROSS_WT,
    build_panel,
)

CRACK_PATH = Path("data/silver/crack_spread.parquet")
COST_SWEEP_BPS = (15.0, 25.0, 50.0, 75.0)
BOOT_DRAWS = 10_000


def _run_staggered_books(
    df: pd.DataFrame,
    side: str,  # "LONG" or "SHORT"
    signal_col: str,
    thresh_mode: str,  # "q80_high" or "q20_low"
    extra_mask_col: str | None,
    cost_rt: float,
) -> tuple[pd.DataFrame, np.ndarray]:
    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    q_cache: dict[pd.Timestamp, float | None] = {}

    def get_cutoff(dt: pd.Timestamp) -> float | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < HOLDING_DAYS:
            q_cache[dt] = None
            return None
        embargo_dt = all_dates[idx - HOLDING_DAYS]
        hist = df[
            (df["trading_date"] <= embargo_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df[signal_col].notna())
            & (df["fwd_ret_10d_excess"].notna())
        ]
        if len(hist) < 60:
            q_cache[dt] = None
            return None
        q_val = 0.80 if thresh_mode == "q80_high" else 0.20
        q_cache[dt] = float(np.quantile(hist[signal_col].values, q_val))
        return q_cache[dt]

    rows: list[dict] = []
    book_step_rets: list[np.ndarray] = []
    sign = 1.0 if side == "LONG" else -1.0

    for offset in range(HOLDING_DAYS):
        eq, peak, dd = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        pnls: list[float] = []
        cur_step_rets: list[float] = []

        for dt in all_dates[offset::HOLDING_DAYS]:
            idx = date_to_idx[dt]
            if idx + HOLDING_DAYS >= len(all_dates):
                continue
            cut = get_cutoff(dt)
            if cut is None:
                cur_step_rets.append(0.0)
                continue

            sig_cond = (df[signal_col] >= cut) if thresh_mode == "q80_high" else (df[signal_col] <= cut)
            cond = (
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & sig_cond
                & (df["fwd_ret_10d_excess"].notna())
            )
            if "regime_label" in df.columns:
                cond &= df["regime_label"] == "IDIOSYNCRATIC"
            if extra_mask_col is not None:
                cond &= df[extra_mask_col].fillna(False)

            cands = df[cond]
            if cands.empty:
                cur_step_rets.append(0.0)
                continue

            sc = min(1.0, MAX_GROSS_WT / (len(cands) * EQ_WT_LEG))
            sp = 0.0
            for _, r in cands.iterrows():
                nr = sign * float(r["fwd_ret_10d_excess"]) - cost_rt
                pnl = eq * (EQ_WT_LEG * sc) * nr
                sp += pnl
                pnls.append(pnl)
            cur_step_rets.append(sp / eq)
            eq += sp
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)

        gp = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p < 0))
        pf = gp / gl if gl > 0 else np.nan
        b_arr = np.array(cur_step_rets, dtype=float)
        b_sh = (
            float(np.mean(b_arr) / np.std(b_arr, ddof=1) * np.sqrt(252.0 / HOLDING_DAYS))
            if len(b_arr) > 1 and np.std(b_arr, ddof=1) > 0
            else 0.0
        )
        rows.append(
            {
                "n": len(pnls),
                "net_pct": (eq / INITIAL_EQUITY - 1.0) * 100.0,
                "sh": b_sh,
                "pf": pf,
                "dd_pct": dd * 100.0,
            }
        )
        book_step_rets.append(b_arr)

    res = pd.DataFrame(rows)
    return res, book_step_rets


def block_bootstrap_stats(step_rets: np.ndarray, n_draws: int = BOOT_DRAWS, seed: int = 42) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    clean = step_rets[~np.isnan(step_rets)]
    n = len(clean)
    if n < 10:
        return {"p_val": np.nan, "ci_lo_pct": np.nan, "ci_hi_pct": np.nan, "boot_sh_med": np.nan}

    block_len = max(2, int(round(np.sqrt(n))))
    n_blocks = int(np.ceil(n / block_len))
    boot_sharpes = np.empty(n_draws, dtype=float)
    boot_10d_means = np.empty(n_draws, dtype=float)

    for b in range(n_draws):
        starts = rng.integers(0, n, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) % n for s in starts])[:n]
        sample = clean[idx]
        sd = np.std(sample, ddof=1)
        boot_sharpes[b] = (np.mean(sample) / sd * np.sqrt(252.0 / HOLDING_DAYS)) if sd > 0 else 0.0
        boot_10d_means[b] = np.mean(sample) * 100.0

    # One-sided empirical p-value against zero positive drift
    p_val = float(np.mean(boot_sharpes <= 0.0))
    lo, hi = np.percentile(boot_10d_means, [2.5, 97.5])
    return {
        "p_val": p_val,
        "ci_lo_pct": float(lo),
        "ci_hi_pct": float(hi),
        "boot_sh_med": float(np.median(boot_sharpes)),
    }


def eval_bootstrap_summary(book_step_rets: list[np.ndarray]) -> dict[str, float]:
    p_vals, lo_vals, hi_vals, med_sharpes = [], [], [], []
    for b_arr in book_step_rets:
        stats = block_bootstrap_stats(b_arr)
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


def audit_steel_long_q5() -> None:
    fac_gdf = gpd.read_file(FACILITIES_PATH)
    steel_gdf = fac_gdf[fac_gdf["sector"] == "STEEL"].copy()
    bronze = ingest_industrial_bronze()
    gold = build_industrial_features(bronze, steel_gdf)

    tickers = sorted(steel_gdf["ticker"].unique().tolist())
    raw = yf.download(tickers + ["XME"], start="2018-01-01", auto_adjust=False, progress=False)
    lvl0 = raw.columns.get_level_values(0)
    field = "Adj Close" if "Adj Close" in lvl0 else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    rets = px.pct_change()

    xme_px = px["XME"]
    xme_mom20 = xme_px / xme_px.shift(20) - 1.0
    mom_df = pd.DataFrame({"trading_date": px.index, "xme_mom20_pos": (xme_mom20 > 0.0).values})

    rows = []
    for tkr in tickers:
        if tkr not in px.columns:
            continue
        beta = rets[tkr].rolling(BETA_WINDOW, min_periods=30).cov(rets["XME"]) / rets["XME"].rolling(
            BETA_WINDOW, min_periods=30
        ).var().replace(0.0, np.nan)
        fwd_s = px[tkr].shift(-HOLDING_DAYS) / px[tkr] - 1.0
        fwd_e = xme_px.shift(-HOLDING_DAYS) / xme_px - 1.0
        rows.append(
            pd.DataFrame(
                {
                    "trading_date": px.index,
                    "ticker": tkr,
                    "fwd_ret_10d_excess": (fwd_s - beta * fwd_e).values,
                }
            )
        )
    mkt = pd.concat(rows, ignore_index=True).dropna(subset=["fwd_ret_10d_excess"])
    df = gold.merge(mkt, on=["ticker", "trading_date"], how="inner").merge(mom_df, on="trading_date", how="left")

    all_dates = sorted(df["trading_date"].unique())
    no_sub = df[
        df["trading_date"].isin(set(all_dates[::HOLDING_DAYS]))
        & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
        & df["yoy_per_pass"].notna()
        & df["fwd_ret_10d_excess"].notna()
    ]
    ic, p_val = spearmanr(no_sub["yoy_per_pass"], no_sub["fwd_ret_10d_excess"])
    t_stat = ic * np.sqrt((len(no_sub) - 2) / max(1e-12, 1.0 - ic**2))
    print(f"\n=== STEEL (CLF, NUE, STLD vs XME) | Non-Overlap 10d IC={ic:+.4f} (t={t_stat:+.2f}, p={p_val:.4f}, n={len(no_sub)}) ===")

    for label, mask_col in [
        ("Long Q5 Steel (All)", None),
        ("Long Q5 Steel + XME 20d Mom > 0", "xme_mom20_pos"),
    ]:
        books, book_steps = _run_staggered_books(
            df, side="LONG", signal_col="yoy_per_pass", thresh_mode="q80_high", extra_mask_col=mask_col, cost_rt=0.0025
        )
        boot = eval_bootstrap_summary(book_steps)
        print(
            f"  {label:32s} | AvgN={books['n'].mean():4.1f} | Net={books['net_pct'].mean():+6.2f}% "
            f"| Sh={books['sh'].mean():+.2f} | PF={books['pf'].mean():.2f} "
            f"| DD={books['dd_pct'].mean():4.2f}% | Pos={(books['net_pct']>0).sum():2d}/10 "
            f"| MedBootP={boot['med_p_val']:.3f} | 95%CI=[{boot['med_ci_lo']:+.2f}%, {boot['med_ci_hi']:+.2f}%]"
        )


def audit_refiner_stress() -> None:
    df = build_panel()
    if CRACK_PATH.exists():
        crack = pd.read_parquet(CRACK_PATH)[["trading_date", "crack_z60"]].sort_values("trading_date")
        df = pd.merge_asof(df.sort_values("trading_date"), crack, on="trading_date", direction="backward")
        df["crack_ok"] = df["crack_z60"] <= 1.0
        mask_col: str | None = "crack_ok"
    else:
        mask_col = None

    print("\n=== REFINER SHORT Q5 + CRACK<=1.0 COST & BLOCK-BOOTSTRAP STRESS TEST (10 BOOKS) ===")
    for bps in COST_SWEEP_BPS:
        books, book_steps = _run_staggered_books(
            df,
            side="SHORT",
            signal_col="yoy_per_pass",
            thresh_mode="q80_high",
            extra_mask_col=mask_col,
            cost_rt=bps / 1e4,
        )
        boot = eval_bootstrap_summary(book_steps)
        print(
            f"  Cost={bps:4.0f} bps RT | AvgN={books['n'].mean():4.1f} | Net={books['net_pct'].mean():+6.2f}% "
            f"| Sh={books['sh'].mean():+.2f} | PF={books['pf'].mean():.2f} "
            f"| DD={books['dd_pct'].mean():4.2f}% | Pos={(books['net_pct']>0).sum():2d}/10 "
            f"| MedBootP={boot['med_p_val']:.3f} | 95%CI=[{boot['med_ci_lo']:+.2f}%, {boot['med_ci_hi']:+.2f}%]"
        )


if __name__ == "__main__":
    audit_steel_long_q5()
    audit_refiner_stress()
