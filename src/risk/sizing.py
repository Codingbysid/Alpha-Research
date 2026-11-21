"""
Regime-gated Fractional Kelly + Merton Jump-Diffusion Monte Carlo CVaR position sizer.

Usage:
    PYTHONPATH=. python3 src/risk/sizing.py
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

MARKET_PATH = Path("data/silver/market_returns.parquet")
REGIME_PATH = Path("data/silver/market_regimes.parquet")


@dataclass
class MertonParams:
    sigma_diff: float
    lam_jump: float
    mu_jump: float
    sigma_jump: float


@dataclass
class TradeSizing:
    ticker: str
    as_of_date: str
    direction: int
    regime_label: str
    p_macro_panic: float
    raw_kelly: float
    regime_scaled_kelly: float
    unit_var_95: float
    unit_cvar_95: float
    cvar_capped_weight: float
    final_weight: float
    notional_usd: float
    shares: int
    est_trade_cvar_usd: float
    note: str


def calibrate_merton_params(daily_returns: pd.Series, jump_z: float = 3.0) -> MertonParams:
    """Separates diffusive volatility from fat-tail jumps via robust MAD thresholding."""
    r = daily_returns.dropna().values
    if len(r) < 60:
        raise ValueError("Need at least 60 historical return observations to calibrate Merton parameters.")

    med = np.median(r)
    mad_sigma = 1.4826 * np.median(np.abs(r - med))
    jump_mask = np.abs(r - med) > (jump_z * max(mad_sigma, 1e-6))

    diff_r = r[~jump_mask]
    sigma_diff = (
        float(np.std(diff_r, ddof=1) * np.sqrt(252))
        if len(diff_r) > 10
        else float(np.std(r) * np.sqrt(252))
    )

    jumps = r[jump_mask]
    lam_jump = float(len(jumps) / len(r) * 252.0)
    if len(jumps) >= 5:
        log_jumps = np.log1p(np.clip(jumps, -0.95, None))
        mu_jump = float(np.mean(log_jumps))
        sigma_jump = float(np.std(log_jumps, ddof=1))
    else:
        mu_jump, sigma_jump = -0.02, 0.04

    return MertonParams(sigma_diff, lam_jump, mu_jump, sigma_jump)


def simulate_merton_unit_returns(
    params: MertonParams,
    direction: int,
    holding_days: int = 10,
    n_sims: int = 10_000,
    seed: int = 42,
) -> np.ndarray:
    """
    Simulates zero-drift (conservative tail stress) Merton Jump-Diffusion returns
    for a 100%-allocated position over `holding_days`.
    """
    rng = np.random.default_rng(seed)
    dt = 1.0 / 252.0
    k = np.exp(params.mu_jump + 0.5 * params.sigma_jump**2) - 1.0

    drift_dt = (-0.5 * params.sigma_diff**2 - params.lam_jump * k) * dt
    z_diff = rng.standard_normal((n_sims, holding_days))
    diff_step = drift_dt + params.sigma_diff * np.sqrt(dt) * z_diff

    n_jumps = rng.poisson(params.lam_jump * dt, size=(n_sims, holding_days))
    z_jump = rng.standard_normal((n_sims, holding_days))
    jump_step = n_jumps * params.mu_jump + np.sqrt(n_jumps) * params.sigma_jump * z_jump

    asset_ret = np.exp((diff_step + jump_step).sum(axis=1)) - 1.0
    return direction * asset_ret


def size_trade(
    ticker: str,
    direction: int,
    p_win: float,
    payoff_ratio_b: float,
    equity: float,
    as_of_date: str | pd.Timestamp | None = None,
    holding_days: int = 10,
    kelly_fraction: float = 0.25,
    max_cvar_pct: float = 0.015,
    max_pos_pct: float = 0.15,
    n_sims: int = 10_000,
) -> TradeSizing:
    if direction not in (1, -1):
        raise ValueError("direction must be +1 (long) or -1 (short).")
    if payoff_ratio_b <= 0:
        raise ValueError("payoff_ratio_b must be positive.")

    mkt = pd.read_parquet(MARKET_PATH)
    reg = pd.read_parquet(REGIME_PATH)

    t_mkt = mkt[mkt["ticker"] == ticker].sort_values("trading_date")
    if as_of_date is not None:
        cutoff = pd.Timestamp(as_of_date)
        t_mkt = t_mkt[t_mkt["trading_date"] <= cutoff]
        reg = reg[reg["trading_date"] <= cutoff]

    latest_mkt = t_mkt.iloc[-1]
    latest_reg = reg.iloc[-1]
    dt_str = str(pd.Timestamp(latest_mkt["trading_date"]).date())
    px = float(latest_mkt["adj_close"])
    p_panic = float(latest_reg["p_macro_panic"]) if pd.notna(latest_reg["p_macro_panic"]) else 1.0
    regime_label = str(latest_reg["regime_label"])

    # 1. Regime gate: OVX-consistent scaling
    ovx_val = float(latest_reg["ovx_level"]) if pd.notna(latest_reg["ovx_level"]) else np.nan
    if regime_label != "IDIOSYNCRATIC":
        regime_mult = 0.0
    elif pd.notna(ovx_val) and ovx_val < 35.0:
        regime_mult = 1.0
    elif pd.notna(ovx_val) and p_panic >= 0.50:
        regime_mult = float(np.clip(1.0 - 0.5 * ((ovx_val - 35.0) / 10.0), 0.5, 1.0))
    else:
        regime_mult = 1.0

    # 2. Fractional Kelly
    raw_kelly = p_win - (1.0 - p_win) / payoff_ratio_b
    regime_kelly = max(0.0, raw_kelly * kelly_fraction * regime_mult)

    # 3. Merton Jump-Diffusion Monte Carlo 95% CVaR
    params = calibrate_merton_params(t_mkt["ret_1d"])
    unit_rets = simulate_merton_unit_returns(
        params, direction, holding_days=holding_days, n_sims=n_sims
    )
    unit_losses = -unit_rets
    unit_var_95 = float(np.quantile(unit_losses, 0.95))
    unit_cvar_95 = float(unit_losses[unit_losses >= unit_var_95].mean())

    cvar_cap = max_cvar_pct / max(unit_cvar_95, 1e-6)
    final_weight = min(regime_kelly, cvar_cap, max_pos_pct)

    notional = equity * final_weight
    shares = int(notional // px) if px > 0 else 0
    actual_notional = shares * px
    est_cvar_usd = actual_notional * unit_cvar_95

    if regime_mult == 0.0:
        note = f"BLOCKED by HMM regime ({regime_label}, p_panic={p_panic:.2f})"
    elif raw_kelly <= 0:
        note = "NO TRADE (negative Kelly edge)"
    elif final_weight == cvar_cap < regime_kelly:
        note = f"TRIMMED by 95% CVaR cap ({max_cvar_pct*100:.2f}% equity limit)"
    else:
        note = "OK"

    return TradeSizing(
        ticker=ticker,
        as_of_date=dt_str,
        direction=direction,
        regime_label=regime_label,
        p_macro_panic=round(p_panic, 4),
        raw_kelly=round(raw_kelly, 4),
        regime_scaled_kelly=round(regime_kelly, 4),
        unit_var_95=round(unit_var_95, 4),
        unit_cvar_95=round(unit_cvar_95, 4),
        cvar_capped_weight=round(cvar_cap, 4),
        final_weight=round(final_weight, 4),
        notional_usd=round(actual_notional, 2),
        shares=shares,
        est_trade_cvar_usd=round(est_cvar_usd, 2),
        note=note,
    )


if __name__ == "__main__":
    res = size_trade(
        ticker="VLO",
        direction=-1,
        p_win=0.58,
        payoff_ratio_b=1.35,
        equity=25_000.0,
        holding_days=10,
        kelly_fraction=0.25,
        max_cvar_pct=0.015,
    )
    for k, v in asdict(res).items():
        print(f"{k:22s}: {v}")
