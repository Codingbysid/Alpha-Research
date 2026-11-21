"""
Evaluates:
1) 3-2-1 Crack Spread conditioning on Pure Short Q5 across 10 staggered books (net of 25 bps)
2) Whether Friday NASA FIRMS thermal signals front-run Wednesday EIA utilization changes

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_crack_eia.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

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
EIA_PATH = Path("data/silver/eia_weekly.parquet")
SILVER_FAC_PATH = Path("data/silver/facility_daily.parquet")


def run_filtered_books(df: pd.DataFrame, mask_col: str | None, label: str) -> pd.DataFrame:
    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    q80_cache: dict[pd.Timestamp, float | None] = {}

    def get_q80(dt: pd.Timestamp) -> float | None:
        if dt in q80_cache:
            return q80_cache[dt]
        idx = date_to_idx[dt]
        if idx < HOLDING_DAYS:
            q80_cache[dt] = None
            return None
        embargo_dt = all_dates[idx - HOLDING_DAYS]
        hist = df[
            (df["trading_date"] <= embargo_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df["yoy_per_pass"].notna())
            & (df["fwd_ret_10d_excess"].notna())
        ]
        q80_cache[dt] = float(np.quantile(hist["yoy_per_pass"].values, 0.80)) if len(hist) >= 80 else None
        return q80_cache[dt]

    rows, trades = [], []
    for offset in range(HOLDING_DAYS):
        eq, peak, dd = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        step_r, pnls = [], []
        for dt in all_dates[offset::HOLDING_DAYS]:
            if date_to_idx[dt] + HOLDING_DAYS >= len(all_dates):
                continue
            q80 = get_q80(dt)
            if q80 is None:
                step_r.append(0.0)
                continue

            cond = (
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df["regime_label"] == "IDIOSYNCRATIC")
                & (df["yoy_per_pass"] >= q80)
                & (df["fwd_ret_10d_excess"].notna())
            )
            if mask_col is not None:
                cond &= df[mask_col].fillna(False)

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
        rows.append(
            {
                "n": len(pnls),
                "net%": (eq / INITIAL_EQUITY - 1.0) * 100,
                "exB%": ex_b,
                "sh": sh,
                "pf": pf,
                "dd%": dd * 100,
            }
        )

    res = pd.DataFrame(rows)
    tdf = pd.DataFrame(trades)
    wr = (tdf["net_ret"] > 0).mean() * 100 if not tdf.empty else np.nan
    print(
        f"{label:34s} | AvgN={res['n'].mean():4.1f} | Net={res['net%'].mean():+6.2f}% "
        f"(exB {res['exB%'].mean():+5.2f}%) | Sh={res['sh'].mean():.2f} | PF={res['pf'].mean():.2f} "
        f"| DD={res['dd%'].mean():4.2f}% | WR={wr:4.1f}% | Pos={(res['net%']>0).sum()}/10"
    )
    return tdf


def audit_eia_lead_lag(df: pd.DataFrame) -> None:
    if not EIA_PATH.exists():
        print("\n[eia-audit] Skipped (add EIA_API_KEY to .env and re-run energy_macro.py to enable).")
        return

    eia = pd.read_parquet(EIA_PATH).sort_values("week_ending_friday")
    # Aggregate clear-sky thermal signal by EIA reporting week (ending Friday)
    clear = df[df["cloud_cover_pct_weighted"] <= CLOUD_MAX].copy()
    clear["week_ending_friday"] = clear["trading_date"] + pd.to_timedelta(
        (4 - clear["trading_date"].dt.weekday) % 7, unit="D"
    )
    wk_therm = clear.groupby("week_ending_friday", as_index=False).agg(
        us_yoy_mean=("yoy_per_pass", "mean"),
        us_frp_pp=("w_frp_pp", "mean"),
        clear_obs=("yoy_per_pass", "count"),
    )
    wk_therm["us_frp_wow"] = wk_therm["us_frp_pp"].diff(1)

    m = eia.merge(wk_therm, on="week_ending_friday", how="inner")
    m = m[m["clear_obs"] >= 3]

    print(f"\n=== NASA FRIDAY THERMAL -> WEDNESDAY EIA UTILIZATION FRONT-RUN (n={len(m)} weeks) ===")
    for feat in ("us_yoy_mean", "us_frp_wow"):
        for tgt in ("util_us_pct_wow", "util_padd3_pct_wow", "util_us_pct_surprise", "crude_us_mbbl_wow"):
            if tgt not in m.columns:
                continue
            sub = m[[feat, tgt]].dropna()
            if len(sub) < 25:
                continue
            ic, p = spearmanr(sub[feat], sub[tgt])
            t = ic * np.sqrt((len(sub) - 2) / max(1e-12, 1.0 - ic**2))
            print(f"  {feat:13s} -> {tgt:22s} | n={len(sub):3d} | IC={ic:+.4f} | t={t:+.2f} | p={p:.4f}")

    # Also test PIT EIA utilization filter on equity Short Q5 (strictly using release_date <= trading_date)
    eia_pit = eia[["release_date", "util_us_pct_wow", "util_padd3_pct_wow"]].sort_values("release_date")
    merged = pd.merge_asof(
        df.sort_values("trading_date"),
        eia_pit,
        left_on="trading_date",
        right_on="release_date",
        direction="backward",
    )
    merged["eia_util_falling"] = merged["util_us_pct_wow"] <= 0.0
    print("\n=== PIT EIA UTILIZATION FILTER ON SHORT Q5 (RELEASED WED <= TRADING DATE) ===")
    run_filtered_books(merged, "eia_util_falling", "Short Q5 + EIA Util WoW <= 0")


def main() -> None:
    if not CRACK_PATH.exists():
        raise FileNotFoundError("Run src/ingest/energy_macro.py first.")

    df = build_panel().merge(pd.read_parquet(CRACK_PATH), on="trading_date", how="left")
    df["no_crack_breakout"] = df["crack_z60"] <= 1.0
    df["crack_falling_20d"] = df["crack_chg_20d"] <= 0.0
    df["crack_below_ma20"] = ~df["crack_above_ma20"].fillna(False)
    df["crack_breakout_only"] = df["crack_z60"] > 1.0

    print("=== 3-2-1 CRACK SPREAD FILTER AUDIT ON PURE SHORT Q5 (NET 25 BPS, 10 STAGGERED BOOKS) ===")
    run_filtered_books(df, None, "Baseline: Pure Short Q5 (All)")
    t_no_bo = run_filtered_books(df, "no_crack_breakout", "Filter: crack_z60 <= +1.0")
    run_filtered_books(df, "crack_falling_20d", "Filter: crack_chg_20d <= 0")
    run_filtered_books(df, "crack_below_ma20", "Filter: crack < 20d MA")
    run_filtered_books(df, "crack_breakout_only", "Diagnostic: crack_z60 > +1.0 ONLY")

    if not t_no_bo.empty:
        print("\n=== YEARLY ATTRIBUTION: Short Q5 + crack_z60 <= +1.0 (NET 25 BPS) ===")
        yr = t_no_bo.groupby("year")["net_ret"].agg(
            n="count",
            mean_bps=lambda s: round(s.mean() * 1e4, 1),
            wr_pct=lambda s: round((s > 0).mean() * 100, 1),
        )
        print(yr.to_string())

    audit_eia_lead_lag(df)


if __name__ == "__main__":
    main()

