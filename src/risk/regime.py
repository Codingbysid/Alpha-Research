"""
Causal walk-forward 2-state Gaussian HMM regime filter (Idiosyncratic vs Macro Panic)
with an absolute OVX floor so calm/normal volatility years are not mislabeled as panic.

Usage:
    PYTHONPATH=. python3 src/risk/regime.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from scipy.special import logsumexp
from scipy.stats import multivariate_normal

MARKET_PATH = Path("data/silver/market_returns.parquet")
OUT_PATH = Path("data/silver/market_regimes.parquet")

WARMUP_DAYS = 252
REFIT_EVERY = 63
PANIC_PROB_THRESHOLD = 0.65
PANIC_OVX_FLOOR = 45.0


def _fit_best_hmm(X_scaled: np.ndarray, n_starts: int = 5) -> GaussianHMM:
    best_model: GaussianHMM | None = None
    best_score = -np.inf
    for seed in range(n_starts):
        model = GaussianHMM(
            n_components=2,
            covariance_type="full",
            n_iter=200,
            random_state=seed,
        )
        try:
            model.fit(X_scaled)
            score = model.score(X_scaled)
            if score > best_score:
                best_score = score
                best_model = model
        except Exception:
            continue
    if best_model is None:
        raise RuntimeError("All HMM random initializations failed to converge.")
    return best_model


def _causal_forward_filter(model: GaussianHMM, X_scaled: np.ndarray) -> np.ndarray:
    """Computes strictly causal filtered state probabilities P(S_t | X_{1:t})."""
    n_obs, n_feat = X_scaled.shape
    log_start = np.log(np.clip(model.startprob_, 1e-12, 1.0))
    log_trans = np.log(np.clip(model.transmat_, 1e-12, 1.0))

    log_emit = np.zeros((n_obs, 2))
    jitter = np.eye(n_feat) * 1e-6
    for k in range(2):
        log_emit[:, k] = multivariate_normal.logpdf(
            X_scaled, mean=model.means_[k], cov=model.covars_[k] + jitter
        )

    probs = np.zeros((n_obs, 2))
    log_alpha = log_start + log_emit[0]
    log_alpha -= logsumexp(log_alpha)
    probs[0] = np.exp(log_alpha)

    for t in range(1, n_obs):
        log_alpha = logsumexp(log_alpha[:, None] + log_trans, axis=0) + log_emit[t]
        log_alpha -= logsumexp(log_alpha)
        probs[t] = np.exp(log_alpha)

    return probs


def build_regimes(market_path: Path = MARKET_PATH, out_path: Path = OUT_PATH) -> pd.DataFrame:
    if not market_path.exists():
        raise FileNotFoundError(f"{market_path} not found. Run market_data.py first.")

    mkt = pd.read_parquet(market_path)
    daily = (
        mkt.groupby("trading_date", as_index=False)
        .agg(
            ovx_level=("ovx_level", "first"),
            wti_close=("wti_close", "first"),
            refiner_vol_20d=("vol_20d_ann", "median"),
        )
        .sort_values("trading_date")
        .reset_index(drop=True)
    )

    daily["ovx_level"] = daily["ovx_level"].ffill()
    daily["wti_vol_20d"] = daily["wti_close"].pct_change().rolling(20).std() * np.sqrt(252)

    use_ovx = daily["ovx_level"].notna().mean() > 0.8
    macro_col = "ovx_level" if use_ovx else "wti_vol_20d"

    valid = daily.dropna(subset=[macro_col, "refiner_vol_20d"]).copy()
    valid = valid[(valid[macro_col] > 0) & (valid["refiner_vol_20d"] > 0)].reset_index(drop=True)

    X_raw = np.column_stack(
        [np.log(valid[macro_col].values), np.log(valid["refiner_vol_20d"].values)]
    )
    n = len(valid)
    p_panic = np.full(n, np.nan)

    for start_idx in range(WARMUP_DAYS, n, REFIT_EVERY):
        end_idx = min(start_idx + REFIT_EVERY, n)
        train_raw = X_raw[:start_idx]
        mu, sd = train_raw.mean(axis=0), train_raw.std(axis=0)
        sd = np.where(sd == 0, 1.0, sd)

        model = _fit_best_hmm((train_raw - mu) / sd)
        panic_state = int(np.argmax(model.means_[:, 0]))

        block_probs = _causal_forward_filter(model, (X_raw[:end_idx] - mu) / sd)
        p_panic[start_idx:end_idx] = block_probs[start_idx:end_idx, panic_state]

    valid["p_macro_panic"] = np.round(p_panic, 4)

    # Require both HMM high-vol state probability AND absolute OVX floor (when OVX is used)
    if use_ovx:
        is_panic = (valid["p_macro_panic"] >= PANIC_PROB_THRESHOLD) & (
            valid["ovx_level"] >= PANIC_OVX_FLOOR
        )
    else:
        is_panic = valid["p_macro_panic"] >= PANIC_PROB_THRESHOLD

    valid["regime_label"] = np.where(
        valid["p_macro_panic"].isna(),
        "WARMUP",
        np.where(is_panic, "MACRO_PANIC", "IDIOSYNCRATIC"),
    )
    valid["is_tradeable"] = valid["regime_label"] == "IDIOSYNCRATIC"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "trading_date",
        "ovx_level",
        "refiner_vol_20d",
        "p_macro_panic",
        "regime_label",
        "is_tradeable",
    ]
    valid[cols].to_parquet(out_path, index=False)

    counts = valid["regime_label"].value_counts().to_dict()
    print(
        f"[regime] Saved {len(valid):,} days (macro={macro_col}, ovx_floor={PANIC_OVX_FLOOR}) "
        f"to {out_path} | States: {counts}"
    )

    dt = pd.to_datetime(valid["trading_date"])
    for label, start_d, end_d in [
        ("2019_CALM", "2019-01-01", "2019-12-31"),
        ("2020_SHOCK", "2020-02-15", "2020-05-15"),
        ("2022_SPIKE", "2022-02-01", "2022-07-31"),
    ]:
        win = valid[(dt >= start_d) & (dt <= end_d)]
        if not win.empty:
            panic_share = (win["regime_label"] == "MACRO_PANIC").mean() * 100
            print(
                f"  {label:12s} ({start_d}..{end_d}): {panic_share:5.1f}% MACRO_PANIC "
                f"| mean p_panic={win['p_macro_panic'].mean():.3f}"
            )

    return valid


if __name__ == "__main__":
    df_reg = build_regimes()
    print(df_reg.tail(5).to_string(index=False))
