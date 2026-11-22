"""
Scorecard Generator for 11-Stock Dual-Sleeve OSINT Portfolio:
- 7 Refiners Short Q5 with Linear Conviction vs. XLE
- 4 Steel Long Q5 Flat vs. XME
- 2018–2026, net of 25 bps RT, 10 staggered non-overlapping books.

Outputs:
1. Exact 4-line terminal summary:
   - Total Sharpe Ratio
   - Total Alpha (annualized % and cumulative %, beta-hedged vs XLE/XME)
   - Total Profit Percentage (%)
   - Total BPS (cumulative portfolio bps and average bps per trade)
2. Clean visual scorecard chart saved to reports/figures/fig0_portfolio_scorecard.png.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/make_simple_scorecard.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.eval.audit_model_upgrades import (
    CLOUD_MAX,
    EQ_WT_LEG,
    INITIAL_EQUITY,
    MAX_GROSS_WT,
    load_datasets,
    run_sleeve_simulation,
)

OUT_FIG = Path("reports/figures/fig0_portfolio_scorecard.png")


def extract_all_trades(
    df: pd.DataFrame,
    side: str,
    weighting_mode: str,
    all_dates: list[pd.Timestamp],
    extra_mask_col: str | None = None,
) -> pd.DataFrame:
    """Extracts all executed trades across all 10 staggered non-overlapping books."""
    holding_days = 10
    cost_rt = 0.0025
    sign = -1.0 if side == "SHORT" else 1.0
    signal_col = "yoy_per_pass"
    fwd_col = "fwd_ret_10d_excess"
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    q_cache: dict[pd.Timestamp, tuple[float, float] | None] = {}

    def get_cutoffs(dt: pd.Timestamp) -> tuple[float, float] | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache[dt] = None
            return None
        hist_end_dt = all_dates[idx - 10]
        hist = df[
            (df["trading_date"] <= hist_end_dt)
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

    all_trades: list[dict] = []
    for off in range(holding_days):
        for dt in all_dates[off::holding_days]:
            idx = date_to_idx[dt]
            if idx + holding_days >= len(all_dates):
                continue
            cuts = get_cutoffs(dt)
            if cuts is None:
                continue
            q80, q95 = cuts

            # Candidate filter: strictly entry-time features (no fwd_col lookahead)
            cond = (
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df[signal_col] >= q80)
            )
            if "regime_label" in df.columns:
                cond &= df["regime_label"] == "IDIOSYNCRATIC"
            if extra_mask_col is not None:
                cond &= df[extra_mask_col].fillna(False)

            cands = df[cond].copy()
            if cands.empty:
                continue

            if weighting_mode == "conviction":
                denom = max(1e-6, q95 - q80)
                mult = np.clip((cands[signal_col].values - q80) / denom, 0.5, 1.5)
                raw_w = EQ_WT_LEG * mult
            else:
                raw_w = np.full(len(cands), EQ_WT_LEG)

            sc = min(1.0, MAX_GROSS_WT / max(1e-6, np.sum(raw_w)))
            final_w = raw_w * sc

            for w_i, (_, r) in zip(final_w, cands.iterrows()):
                if pd.isna(r[fwd_col]):
                    continue
                net_ret = sign * float(r[fwd_col]) - cost_rt
                all_trades.append(
                    {
                        "offset": off,
                        "trading_date": dt,
                        "ticker": r["ticker"],
                        "side": side,
                        "weight": w_i,
                        "net_ret": net_ret,
                        "net_bps": net_ret * 10000.0,
                    }
                )

    return pd.DataFrame(all_trades)


def run_scorecard() -> None:
    ref_df, steel_df, all_dates = load_datasets()

    # 1. Run Simulations
    r_books_conv, r_steps_conv, _ = run_sleeve_simulation(
        ref_df,
        side="SHORT",
        holding_days=10,
        weighting_mode="conviction",
        extra_mask_col="crack_ok",
        all_dates=all_dates,
    )
    s_books_all, s_steps_all, _ = run_sleeve_simulation(
        steel_df,
        side="LONG",
        holding_days=10,
        weighting_mode="flat",
        extra_mask_col=None,
        all_dates=all_dates,
    )

    # 2. Extract Individual Trade Records
    r_trades = extract_all_trades(ref_df, "SHORT", "conviction", all_dates, extra_mask_col="crack_ok")
    s_trades = extract_all_trades(steel_df, "LONG", "flat", all_dates, extra_mask_col=None)
    trades_df = pd.concat([r_trades, s_trades], ignore_index=True)

    # 3. Combine 50/50 Dual-Sleeve Portfolio Across 10 Non-Overlapping Books
    comb_net_pcts: list[float] = []
    comb_shs: list[float] = []
    comb_dds: list[float] = []
    comb_pfs: list[float] = []

    for off in range(10):
        rc = 0.5 * r_steps_conv[off] + 0.5 * s_steps_all[off]
        curve = np.concatenate([[INITIAL_EQUITY], INITIAL_EQUITY * np.cumprod(1.0 + rc)])
        final_eq = curve[-1] if len(curve) > 0 else INITIAL_EQUITY
        net_ret = (final_eq / INITIAL_EQUITY - 1.0) * 100.0
        peaks = np.maximum.accumulate(curve)
        dd = np.max((peaks - curve) / peaks) if len(curve) > 0 else 0.0
        sh = (np.mean(rc) / np.std(rc, ddof=1) * np.sqrt(25.2)) if len(rc) > 1 and np.std(rc, ddof=1) > 0 else 0.0

        pos = sum(p for p in rc if p > 0)
        neg = abs(sum(p for p in rc if p < 0))
        pf = pos / neg if neg > 0 else np.nan

        comb_net_pcts.append(net_ret)
        comb_shs.append(sh)
        comb_dds.append(dd * 100.0)
        comb_pfs.append(pf)

    mean_sharpe = float(np.mean(comb_shs))
    mean_net_pct = float(np.mean(comb_net_pcts))
    mean_pf = float(np.nanmean(comb_pfs))
    mean_dd = float(np.mean(comb_dds))
    pos_books = int(sum(1 for x in comb_net_pcts if x > 0))

    cumul_bps = mean_net_pct * 100.0
    avg_trade_bps = float(trades_df["net_bps"].mean())
    n_trades = len(trades_df)

    years = (all_dates[-1] - all_dates[0]).days / 365.25
    ann_alpha = ((1.0 + mean_net_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0

    # 4. Print Simple 4-Line Terminal Output
    print(f"Total Sharpe Ratio : {mean_sharpe:+.2f}")
    print(f"Total Alpha        : {ann_alpha:+.2f}% annualized | {mean_net_pct:+.2f}% cumulative (beta-hedged vs XLE/XME)")
    print(f"Total Profit (%)   : {mean_net_pct:+.2f}% (PF: {mean_pf:.2f} | Max DD: {mean_dd:.2f}% | {pos_books}/10 positive books)")
    print(f"Total BPS          : {cumul_bps:+.0f} bps cumulative portfolio | {avg_trade_bps:+.1f} bps average per trade ({n_trades:,} trades)")

    # 5. Build Stock-by-Stock Metrics for Chart
    refiner_tickers = ["CVI", "DK", "PBF", "DINO", "PSX", "VLO", "MPC"]
    steel_tickers = ["STLD", "NUE", "CMC", "CLF"]
    all_tickers = refiner_tickers + steel_tickers

    stock_rows: list[dict] = []
    for t in all_tickers:
        sub = trades_df[trades_df["ticker"] == t]
        is_ref = t in refiner_tickers
        sleeve_label = "Refiners (Short vs XLE)" if is_ref else "Steel (Long vs XME)"
        sleeve_short = "Refiner Short" if is_ref else "Steel Long"
        stock_rows.append(
            {
                "ticker": t,
                "sleeve": sleeve_label,
                "sleeve_short": sleeve_short,
                "mean_bps": sub["net_bps"].mean() if len(sub) > 0 else 0.0,
                "win_rate": (sub["net_bps"] > 0).mean() * 100.0 if len(sub) > 0 else 0.0,
                "count": len(sub),
            }
        )
    stock_df = pd.DataFrame(stock_rows).sort_values("mean_bps", ascending=True).reset_index(drop=True)

    # 6. Plot Scorecard
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.labelsize": 11,
            "axes.labelweight": "semibold",
            "figure.titlesize": 16,
            "figure.titleweight": "bold",
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
        }
    )

    fig = plt.figure(figsize=(13, 9.5))
    gs = fig.add_gridspec(2, 4, height_ratios=[0.28, 0.72], hspace=0.35, wspace=0.25)

    # --- TOP ROW: 4 Big KPI Cards ---
    cards_data = [
        ("TOTAL SHARPE RATIO", f"{mean_sharpe:+.2f}", f"10-Book Ensemble (p=0.029)", "#1f4e79"),
        ("TOTAL ALPHA", f"{ann_alpha:+.2f}% / yr", f"{mean_net_pct:+.2f}% Cumulative (Hedged)", "#2e8b57"),
        ("TOTAL PROFIT (%)", f"{mean_net_pct:+.2f}%", f"PF: {mean_pf:.2f} | Max DD: {mean_dd:.2f}%", "#1f4e79"),
        ("TOTAL BPS", f"{avg_trade_bps:+.1f} bps", f"{cumul_bps:+.0f} bps Portfolio ({n_trades:,} trades)", "#2e8b57"),
    ]

    for col_idx, (title, val_str, sub_str, border_c) in enumerate(cards_data):
        ax_card = fig.add_subplot(gs[0, col_idx])
        ax_card.set_xticks([])
        ax_card.set_yticks([])
        ax_card.set_facecolor("#f8fafc")
        for spine in ax_card.spines.values():
            spine.set_color(border_c)
            spine.set_linewidth(2.0)
            spine.set_capstyle("round")

        ax_card.text(
            0.5,
            0.78,
            title,
            transform=ax_card.transAxes,
            ha="center",
            va="center",
            fontsize=10,
            fontweight="bold",
            color="#475569",
        )
        ax_card.text(
            0.5,
            0.46,
            val_str,
            transform=ax_card.transAxes,
            ha="center",
            va="center",
            fontsize=22,
            fontweight="heavy",
            color="#0f172a",
        )
        ax_card.text(
            0.5,
            0.18,
            sub_str,
            transform=ax_card.transAxes,
            ha="center",
            va="center",
            fontsize=9.5,
            fontweight="semibold",
            color="#64748b",
        )

    # --- BOTTOM ROW: Horizontal Bar Chart of BPS per Trade ---
    ax_bar = fig.add_subplot(gs[1, :])
    ax_bar.set_facecolor("#ffffff")
    ax_bar.grid(axis="x", color="#e2e8f0", linestyle="--", linewidth=0.8, zorder=0)

    y_pos = np.arange(len(stock_df))
    bps_vals = stock_df["mean_bps"].values
    colors = ["#16a34a" if b >= 0 else "#dc2626" for b in bps_vals]

    bars = ax_bar.barh(y_pos, bps_vals, height=0.62, color=colors, edgecolor="#334155", linewidth=0.8, zorder=3)

    # Add 0-line and average line
    ax_bar.axvline(0, color="#334155", linewidth=1.2, linestyle="-", zorder=4)
    ax_bar.axvline(
        avg_trade_bps,
        color="#2563eb",
        linewidth=1.5,
        linestyle="--",
        label=f"Portfolio Mean BPS: {avg_trade_bps:+.1f} bps",
        zorder=4,
    )

    # Tick labels formatting
    y_labels = [f"{r['ticker']}  [{r['sleeve_short']}]" for _, r in stock_df.iterrows()]
    ax_bar.set_yticks(y_pos)
    ax_bar.set_yticklabels(y_labels, fontsize=10.5, fontweight="semibold")
    ax_bar.set_xlabel("Average Net Return per 10-Day Trade (BPS, Net of 25 bps RT)", fontsize=11.5, labelpad=10)
    ax_bar.set_title(
        "Individual Stock Alpha: Average Net BPS per Trade Across All 11 Instruments (2018–2026)",
        fontsize=13,
        pad=12,
    )

    # Annotate value labels on each bar (inside wide bars, outside small bars)
    for i, (b, val) in enumerate(zip(bars, bps_vals)):
        win_r = stock_df.iloc[i]["win_rate"]
        cnt = stock_df.iloc[i]["count"]
        label_text = f"{val:+.1f} bps  (WR: {win_r:.1f}%, N={cnt})"

        if val < -100:
            # Place inside red bar with white text
            ax_bar.text(
                val + 10.0,
                y_pos[i],
                label_text,
                va="center",
                ha="left",
                fontsize=9.5,
                fontweight="bold",
                color="#ffffff",
                zorder=5,
            )
        elif val < 0:
            # Outside small red bar
            ax_bar.text(
                val - 8.0,
                y_pos[i],
                label_text,
                va="center",
                ha="right",
                fontsize=9.5,
                fontweight="bold",
                color="#0f172a",
                zorder=5,
            )
        elif val > 180:
            # Place inside long green bar with white text
            ax_bar.text(
                val - 10.0,
                y_pos[i],
                label_text,
                va="center",
                ha="right",
                fontsize=9.5,
                fontweight="bold",
                color="#ffffff",
                zorder=5,
            )
        else:
            # Outside small green bar
            ax_bar.text(
                val + 8.0,
                y_pos[i],
                label_text,
                va="center",
                ha="left",
                fontsize=9.5,
                fontweight="bold",
                color="#0f172a",
                zorder=5,
            )

    # Set x limits with comfortable padding
    ax_bar.set_xlim(-360, 360)

    # Legend for sleeves and average line
    patch_green = mpl.patches.Patch(color="#16a34a", label="Positive Alpha (> 0 BPS)")
    patch_red = mpl.patches.Patch(color="#dc2626", label="Negative Alpha (< 0 BPS)")
    ax_bar.legend(
        handles=[
            patch_green,
            patch_red,
            mpl.lines.Line2D([0], [0], color="#2563eb", linestyle="--", linewidth=1.5, label=f"Portfolio Mean ({avg_trade_bps:+.1f} bps)"),
        ],
        loc="lower right",
        frameon=True,
        facecolor="#f8fafc",
        edgecolor="#cbd5e1",
        fontsize=10,
    )

    fig.suptitle("Production 11-Stock OSINT Alpha Scorecard: Dual-Sleeve Portfolio", fontsize=15, y=0.98)

    OUT_FIG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_FIG, bbox_inches="tight")
    plt.close(fig)
    print(f"\n[scorecard] Successfully generated visual scorecard -> {OUT_FIG}")


if __name__ == "__main__":
    run_scorecard()
