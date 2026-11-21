"""
Purged walk-forward ML model (Ridge + HistGradientBoosting blend) on the 7-refiner dataset.
Evaluates OOS Spearman IC and 10 staggered non-overlapping sub-books (net of 25 bps RT).

Usage:
    PYTHONPATH=. python3 -W ignore src/models/train_ml.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from src.eval.audit_short_q5 import (
    CLOUD_MAX,
    COST_RT,
    EQ_WT_LEG,
    HOLDING_DAYS,
    INITIAL_EQUITY,
    MAX_GROSS_WT,
    build_panel,
)

CRACK_PATH = Path("data/silver/crack_spread.parquet")
REFIT_EVERY = 63
MIN_TRAIN_ROWS = 250

FEATURE_COLS = [
    "yoy_per_pass",
    "yoy_cs_rank",
    "yoy_5d_mean",
    "log_w_frp_pp",
    "frp_zscore_30d",
    "frp_zscore_90d",
    "clear_sky_zero_cap_share",
    "crack_z60",
    "crack_chg_20d",
    "ovx_level",
    "vol_20d_ann",
]


def prepare_ml_frame() -> pd.DataFrame:
    df = build_panel()
    crack = pd.read_parquet(CRACK_PATH)[["trading_date", "crack_z60", "crack_chg_20d"]].sort_values(
        "trading_date"
    )
    df = pd.merge_asof(df.sort_values("trading_date"), crack, on="trading_date", direction="backward")

    frames = []
    for _, g in df.groupby("ticker"):
        g = g.sort_values("trading_date").copy()
        g["log_w_frp_pp"] = np.log1p(g["w_frp_pp"].clip(lower=0.0))
        g["yoy_5d_mean"] = g["yoy_per_pass"].rolling(5, min_periods=2).mean()
        frames.append(g)
    df = pd.concat(frames, ignore_index=True)

    # Cross-sectional rank across refiners on the same day (strips out regional sensor drift)
    df["yoy_cs_rank"] = df.groupby("trading_date")["yoy_per_pass"].rank(pct=True)
    return df.sort_values(["trading_date", "ticker"]).reset_index(drop=True)


def generate_oos_predictions(df: pd.DataFrame) -> pd.DataFrame:
    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    # Train only on clear-sky rows with valid targets
    clear_mask = (
        (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
        & (df["yoy_per_pass"].notna())
        & (df["fwd_ret_10d_excess"].notna())
    )

    preds = np.full(len(df), np.nan)
    first_valid_dt = df.loc[clear_mask, "trading_date"].min()
    start_idx = max(date_to_idx[first_valid_dt] + 120, HOLDING_DAYS + 1)

    for block_start in range(start_idx, len(all_dates), REFIT_EVERY):
        block_end = min(block_start + REFIT_EVERY, len(all_dates))
        embargo_dt = all_dates[block_start - HOLDING_DAYS]

        train_df = df[clear_mask & (df["trading_date"] <= embargo_dt)]
        if len(train_df) < MIN_TRAIN_ROWS:
            continue

        X_tr = train_df[FEATURE_COLS].values
        y_tr = train_df["fwd_ret_10d_excess"].clip(-0.25, 0.25).values

        # Shallow regularized tree + linear Ridge blend (prevents overfitting on ~1k-3k rows)
        hgb = HistGradientBoostingRegressor(
            max_depth=3,
            max_leaf_nodes=8,
            min_samples_leaf=40,
            learning_rate=0.03,
            max_iter=120,
            l2_regularization=5.0,
            random_state=42,
        )
        ridge = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=50.0))

        hgb.fit(X_tr, y_tr)
        ridge.fit(X_tr, y_tr)

        block_dates = set(all_dates[block_start:block_end])
        test_idx = df.index[df["trading_date"].isin(block_dates) & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)]
        if len(test_idx) == 0:
            continue

        X_te = df.loc[test_idx, FEATURE_COLS].values
        preds[test_idx] = 0.5 * hgb.predict(X_te) + 0.5 * ridge.predict(X_te)

    out = df.copy()
    out["ml_pred_10d"] = preds
    return out


def evaluate_ml_books(df: pd.DataFrame, crack_gate: bool = True, label: str = "ML Short Q1 (Bottom 20% Pred)") -> None:
    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    # Honest non-overlapping OOS Spearman IC
    no_dates = set(all_dates[::HOLDING_DAYS])
    oos = df[df["trading_date"].isin(no_dates) & df["ml_pred_10d"].notna() & df["fwd_ret_10d_excess"].notna()]
    ic, p_val = spearmanr(oos["ml_pred_10d"], oos["fwd_ret_10d_excess"])
    print(f"[ml-oos] Non-overlapping 10d Spearman IC = {ic:+.4f} (p={p_val:.4f}, n={len(oos)})")

    rows, trades = [], []
    for offset in range(HOLDING_DAYS):
        eq, peak, dd = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        step_r, pnls = [], []

        for dt in all_dates[offset::HOLDING_DAYS]:
            idx = date_to_idx[dt]
            if idx < HOLDING_DAYS or idx + HOLDING_DAYS >= len(all_dates):
                continue

            # Causal 20th percentile of past OOS predictions (most negative predicted returns)
            embargo_dt = all_dates[idx - HOLDING_DAYS]
            hist_p = df.loc[(df["trading_date"] <= embargo_dt) & df["ml_pred_10d"].notna(), "ml_pred_10d"]
            if len(hist_p) < 60:
                step_r.append(0.0)
                continue
            q20_pred = float(np.quantile(hist_p.values, 0.20))

            cond = (
                (df["trading_date"] == dt)
                & (df["regime_label"] == "IDIOSYNCRATIC")
                & (df["ml_pred_10d"] <= q20_pred)
                & (df["fwd_ret_10d_excess"].notna())
            )
            if crack_gate:
                cond &= df["crack_z60"] <= 1.0

            cands = df[cond]
            if cands.empty:
                step_r.append(0.0)
                continue

            sc = min(1.0, MAX_GROSS_WT / (len(cands) * EQ_WT_LEG))
            sp = 0.0
            for _, r in cands.iterrows():
                nr = -float(r["fwd_ret_10d_excess"]) - COST_RT
                pnl = eq * (EQ_WT_LEG * sc) * nr
                sp += pnl
                pnls.append(pnl)
                trades.append({"year": pd.Timestamp(dt).year, "ticker": r["ticker"], "net_ret": nr})
            step_r.append(sp / eq)
            eq += sp
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)

        a = np.array(step_r)
        sh = (a.mean() / a.std(ddof=1) * np.sqrt(252.0 / HOLDING_DAYS)) if a.std(ddof=1) > 0 else 0.0
        gp, gl = sum(p for p in pnls if p > 0), abs(sum(p for p in pnls if p < 0))
        pf = gp / gl if gl > 0 else np.nan
        ex_b = ((eq - max(pnls)) / INITIAL_EQUITY - 1.0) * 100 if pnls else 0.0
        rows.append({"n": len(pnls), "net%": (eq / INITIAL_EQUITY - 1.0) * 100, "exB%": ex_b, "sh": sh, "pf": pf, "dd%": dd * 100})

    res = pd.DataFrame(rows)
    tdf = pd.DataFrame(trades)
    wr = (tdf["net_ret"] > 0).mean() * 100 if not tdf.empty else np.nan
    print(
        f"{label:34s} | AvgN={res['n'].mean():4.1f} | Net={res['net%'].mean():+6.2f}% "
        f"(exB {res['exB%'].mean():+5.2f}%) | Sh={res['sh'].mean():.2f} | PF={res['pf'].mean():.2f} "
        f"| DD={res['dd%'].mean():4.2f}% | WR={wr:4.1f}% | Pos={(res['net%']>0).sum()}/10"
    )


def main() -> None:
    df = prepare_ml_frame()
    df_oos = generate_oos_predictions(df)
    print("\n=== PURGED WALK-FORWARD ML vs BASELINE (NET 25 BPS, 10 STAGGERED BOOKS) ===")
    evaluate_ml_books(df_oos, crack_gate=False, label="ML Short Bottom-20% (No Crack)")
    evaluate_ml_books(df_oos, crack_gate=True, label="ML Short Bottom-20% + crack<=1")


if __name__ == "__main__":
    main()
