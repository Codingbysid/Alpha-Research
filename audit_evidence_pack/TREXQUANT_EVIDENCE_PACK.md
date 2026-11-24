# Trexquant Research Presentation Evidence Pack
**Physical Thermal OSINT Alpha: Refining Distress (Short Q5 vs XLE) & Steel Throughput (Long Q5 vs XME)**

---

## 1. Provenance, Environment & Data Isolation Record

* **Execution Workspace:** Isolated directory `audit_evidence_pack/` (leaving `src/live/`, `data/live/`, AWS, and GitHub 100% frozen).
* **Git Commit Hash:** `3dfb08e` (`feat(live): integrate validated causal 60d residual-vol haircut sizing (p=0.009) into live runner and deploy to Lambda`)
* **Local Workspace Differences:** `reports/figures/fig0_portfolio_scorecard.png` (uncommitted binary figure update).
* **Evaluation Date Range:** `2018-04-02` to `2026-11-20` (2,174 shared trading days) + live paper session `2026-11-23`.
* **Input Data Artifacts & Versions:**
  - `data/silver/market_returns.parquet` (7 refiners: `CVI`, `DINO`, `DK`, `MPC`, `PBF`, `PSX`, `VLO`)
  - `data/silver/xle_close.parquet` (Sector ETF hedge benchmark)
  - `data/silver/crack_spread.parquet` (Gulf Coast 3:2:1 crack spread & 60d trailing z-score)
  - `data/silver/market_regimes.parquet` (Causal 2-state HMM, panic probability $p_{\text{panic}}$, OVX)
  - `data/silver/cloud_cover.parquet` (Refinery cloud cover weights from ERA5/NASA POWER)
  - `data/gold/refinery_features.parquet` (VIIRS nighttime FRP per pass)
  - `data/industrial/bronze_industrial.parquet` (VIIRS detections 2018–2026 for metals & chemicals, 324,524 rows)
  - `data/industrial/cloud_industrial.parquet` (Industrial cloud cover weights)
  - `audit_evidence_pack/market_prices.parquet` (Cached Yahoo Finance adjusted closes for all 11 stocks + XLE + XME + SPY)

---

## 2. Replay & Dual-Sleeve Portfolio Master Table (Requirements 1 & 3)

### Execution Protocol & Sizing Rules
* **Trading Calendar:** Exact synchronized business days (2018–2026).
* **Execution Timing:** $T+1$ Close execution (signal observed at date $t$ close / 15:50 ET, entered at date $t+1$ close).
* **Holding Horizon:** Strictly 10 trading sessions; exited at close of session $t+11$.
* **Transaction Frictions:** 25 bps round-trip (12.5 bps entry, 12.5 bps exit) baseline; 75 bps stress test.
* **Capital Denominator:** Unified **\$100,000.00** starting portfolio capital base across all series.
* **Paper-Runner Sizing Rules:**
  - **Refiners Short Sleeve:** Base Merton CVaR weight ($5\%$) $\times$ Conviction Multiplier ($[0.5, 1.5]$) $\times$ Causal Trailing 60d Residual-Vol Haircut ($\min(1.0, \text{median}(\sigma)/\sigma_i)$) $\times$ Macro Regime Multiplier ($reg\_mult \in [0.0, 1.0]$), subject to `MAX_SINGLE_WT = 10%` and `MAX_GROSS_WT = 80%`. Single position per ticker (skips if already open).
  - **Steel Long Sleeve:** Unfiltered Flat Equal-Weight ($10\%$ notional per name), subject to `MAX_GROSS_WT = 40%`. Single position per ticker.
  - **Reference Book:** 10-book overlapping simulator (1/10th capital per book) with causal residual-vol haircut.

### Master Results Table

| Strategy / Sleeve | Net Ret (%) | Sharpe | Max DD (%) | Episodes | Worst Day (%) | Stk Gross (%) | ETF Gross (%) | 75 bps Sh | 75 bps Ret (%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Refiners (Research 10-Book Reference)** | `+4.14%` | **`+0.63`** | **`1.30%`** | 563 | `-0.31%` | 1.5% | 1.6% | `+0.51` | `+2.50%` |
| **Refiners (Paper Single-Position)** | `+8.36%` | **`+0.40`** | `4.46%` | 207 | `-0.80%` | 5.2% | 6.0% | `+0.13` | `+2.69%` |
| **Steel (Paper Single-Position)** | `+8.86%` | **`+0.33`** | `6.17%` | 182 | `-1.11%` | 8.4% | 7.7% | `-0.01` | `-0.24%` |
| **COMBINED DUAL-SLEEVE (PAPER POLICY)** | **`+17.22%`** | **`+0.51`** | **`6.42%`** | **389** | **`-1.17%`** | **13.6%** | **13.7%** | **`+0.07`** | **`+2.45%`** |

> [!NOTE]
> **Key Portfolio Insights:**
> 1. **Orthogonal Diversification:** Daily return correlation between Refiners and Steel is **$r = -0.0047$ ($p = 0.8270$)**, confirming complete statistical orthogonality.
> 2. **Sharpe Expansion:** Because the sleeves are uncorrelated, the combined portfolio Sharpe (**`+0.51`**) exceeds both individual live sleeves (`+0.40` and `+0.33`).
> 3. **Cost Resilience:** Even when transaction costs are tripled to **75 bps round-trip**, the combined paper-policy portfolio remains positive (Sharpe `+0.07`, Net Return `+2.45%`).

### Point-in-Time Rule Replay Limitations
1. **Weather Cloud Reanalysis vs. Live Forecast:** Historical backtests use daily ERA5/NASA POWER reanalysis (published with 1–3 day lag). Live paper trading queries morning satellite reanalysis feeds.
2. **Execution Timing & Fill Slippage:** Replays assume execution at exact adjusted close (`adj_close`). Live Alpaca paper orders execute via MOC/market orders at 15:50 ET, subject to real-time bid-ask spread and small execution drift (e.g. `STLD` filled at \$215.49 vs \$215.35 estimated).
3. **Short Locate Availability:** Large-cap refiners in XLE are easy-to-borrow; historical short borrow availability is assumed non-binding.

---

## 3. Controlled 2x2 Evaluation for Steel Sleeve (Requirement 2)

A 2x2 factorial grid was executed across all 4 US Steel constituents (`CLF`, `NUE`, `STLD`, `CMC`):
* **Axis 1 (Coordinates):** Original (`config/facilities_industrial.geojson`) vs. Verified (`audit_evidence_pack/facilities_steel_verified.geojson`).
* **Axis 2 (Denominator):** All detected passes (`acq_datetime_utc.nunique()`) vs. Nighttime-only passes (`night_acq_datetime.nunique()`).

### Table 2A: 2x2 Portfolio-Level Comparison

| Variant | Matched Dets | Q5 Triggers | Episodes | Sharpe | Cum Ret (%) | Max DD (%) | Avg Gross (%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Orig_Coord × All_Pass** | 114,173 | 408 | 223 | `+0.29` | `+9.18%` | 17.67% | 10.2% |
| **Orig_Coord × Night_Pass** | 114,173 | 413 | 223 | `+0.40` | `+12.91%` | 15.22% | 10.2% |
| **Verif_Coord × All_Pass** | 202,837 | 259 | 167 | `+0.55` | `+16.65%` | 9.61% | 7.6% |
| **Verif_Coord × Night_Pass** | 202,837 | 259 | 165 | `+0.55` | `+16.57%` | 9.65% | 7.5% |

### Table 2B: Ticker-by-Ticker Breakdown Across All 4 Cells

| Variant | Ticker | Matched Dets | Q5 Triggers | Episodes | Win Rate (%) | Mean BPS | Total BPS |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Orig_Coord × All_Pass** | `CLF` | 90,520 | 137 | 73 | 53.4% | `+156.2` | `+11,402.4` |
| | `NUE` | 235 | 48 | 29 | 48.3% | `-0.9` | `-26.1` |
| | `STLD` | 16,444 | 155 | 77 | 48.1% | `+29.8` | `+2,297.5` |
| | `CMC` | 6,974 | 68 | 44 | 40.9% | `-98.7` | `-4,343.4` |
| **Orig_Coord × Night_Pass** | `CLF` | 90,520 | 137 | 73 | 53.4% | `+164.5` | `+12,007.3` |
| | `NUE` | 235 | 48 | 29 | 48.3% | `-0.9` | `-26.1` |
| | `STLD` | 16,444 | 159 | 79 | 49.4% | `+55.3` | `+4,372.2` |
| | `CMC` | 6,974 | 69 | 42 | 42.9% | `-78.0` | `-3,275.6` |
| **Verif_Coord × All_Pass** | `CLF` | 103,050 | 135 | 76 | 53.9% | `+161.5` | `+12,277.4` |
| | `NUE` | 49,370 | 39 | 30 | 50.0% | **`+35.8`** | **`+1,074.1`** |
| | `STLD` | 32,374 | 58 | 39 | 48.7% | **`+100.6`** | `+3,921.7` |
| | `CMC` | 18,043 | 27 | 22 | 50.0% | **`-23.0`** | **`-507.0`** |
| **Verif_Coord × Night_Pass** | `CLF` | 103,050 | 136 | 76 | 53.9% | `+169.0` | `+12,842.4` |
| | `NUE` | 49,370 | 39 | 29 | 48.3% | **`+32.3`** | **`+937.3`** |
| | `STLD` | 32,374 | 57 | 38 | 47.4% | **`+89.9`** | `+3,416.1` |
| | `CMC` | 18,043 | 27 | 22 | 50.0% | **`-23.0`** | **`-507.0`** |

### Site-Level Driver Analysis: Explaining the Sign Changes
1. **Nucor (`NUE`) Sign Inversion ($-0.9\text{ bps} \rightarrow +35.8\text{ bps}$):**
   - In `Orig_Coord`, NUE's Berkeley (SC) and Crawfordsville (IN) plant coordinates were 4.7 km and 5.7 km away from the actual mill cores, outside their 2.2 km buffer radii. This caused total matched detections to collapse to just 235 across 8 years (43 for Berkeley, 1 for Crawfordsville), turning NUE into pure noise.
   - When verified coordinates were applied (1455 Hagan Ave, Huger SC and 4537 S Nucor Rd, Crawfordsville IN), detections jumped to **49,370**. True electric arc furnace (EAF) throughput was captured, and NUE flipped from **`-26.1 bps` (negative) to `+1,074.1 bps` (positive)**.
2. **Steel Dynamics (`STLD`) Alpha Tripling ($+29.8\text{ bps} \rightarrow +100.6\text{ bps}$):**
   - The original `stld_columbus` coordinate was 5.9 km away from the Golden Triangle flat-rolled mill (11 detections).
   - The verified coordinate (1945 Airport Rd, Columbus MS) added **16,274 detections**, lifting STLD mean net BPS from **`+29.8 bps` to `+100.6 bps`**.
3. **Commercial Metals (`CMC`) Loss Contraction ($-4,343\text{ bps} \rightarrow -507\text{ bps}$):**
   - Seguin TX and Durant OK were previously misplaced by 4.1 km and 4.6 km. Correcting coordinates reduced CMC's drag by **$88\%$**.

---

## 4. Traceable Paper Decision Audit (Requirement 4)

### Prospective Paper Trading Observations (Monday, Nov 23, 2026 Run)

#### A. Refiners Short Sleeve (Systematic Macro Panic Suppression)
* **Observation Time:** `2026-11-23 15:50:14 ET` (20:50:14 UTC)
* **Execution Telemetry:** AWS Lambda `osint-short-q5-daily`, CloudWatch log stream `2026/11/23/[$LATEST]64c0e3a8271640a38ef4ca19be7ed100`. Runtime: 90.28s, RAM: 1,668 MB.
* **Sensor Feed Status:** NASA FIRMS VIIRS NRT (`feed_ok = True`, 100% tile requests succeeded).
* **Gate / Trigger Evaluation:**
  - Crack Spread Gate: $z_{60} = -1.92 \le 1.0$ (PASS).
  - Macro Regime Gate: Classified as `MACRO_PANIC` ($p_{\text{panic}} = 1.000$, $\text{OVX} = 54.12 \ge 45.0$).
  - Multiplier: `reg_mult = 0.00`.
  - Causal 60d Residual-Vol Multipliers: `CVI`: $0.624\times$, `DK`: $0.683\times$, `PBF`: $0.613\times$, `DINO/MPC/PSX/VLO`: $1.000\times$.
* **Order Time:** N/A (Order generation suppressed).
* **Broker Action & Reason Skipped:** **SKIPPED (0 new entries)**. Systematic circuit breaker active (`reg_mult = 0.00`), successfully preventing short refiner entries during extreme macro crude oil volatility.

#### B. Steel Long Sleeve (Active Market Entry on Alpaca Paper)
* **Observation Time:** `2026-11-23 15:50:14 ET` (20:50:14 UTC)
* **Gate / Trigger Evaluation:**
  - Gate: Unfiltered Flat Equal-Weight ($\text{XME 20d Mom} = -11.63\%$, Market: OPEN).
  - Model Cutoff: $Q_{80} = 1.442$.
  - Trigger: `STLD` (Steel Dynamics) observed thermal intensity $\text{YoY} = 1.536 \ge Q_{80} = 1.442$ ($\text{Cloud Cover} = 0.0\%$).
* **Order Routing Time:** `2026-11-23 15:51:24 ET`
* **Broker-Confirmed Fills (Alpaca Paper Account `PA360O6H5`):**
  - **STLD Long:** Buy 46 shares @ **`$215.49`** (Notional: $\$9,912.54$, $9.91\%$ portfolio weight, respecting $10\%$ cap).
  - **XME Short Hedge:** Sell -84 shares @ **`$84.85`** (Notional: $-\$7,127.40$, sector-neutral beta hedge).
* **Position Status (as of Nov 24, 2026):** Active in ledger, scheduled to hold for 10 sessions (exit on Dec 8, 2026).

---

### Historical Replay Decisions (Exemplary Episodes for Comparison)

#### C. Refiners Historical Replay (Example: `VLO` Short Outage)
* **Signal Observation Date ($t$):** `2024-03-27 Close`
* **Observation State:** `VLO` $\text{YoY} = 2.45 \ge Q_{80} = 1.38$ ($Q_{95} = 2.31$, Conviction $1.50\times$, Vol Mult $1.00\times$, Cloud $12.4\%$, Regime `IDIOSYNCRATIC`, Crack $z_{60} = -0.42$).
* **Order Entry ($t+1$):** `2024-03-28 Close`
* **Execution:** Short `VLO` @ $\$171.25$ ($10.0\%$ notional), Long `XLE` hedge @ $\$94.10$ ($\beta = 1.15$).
* **Exit Date:** `2024-04-12 Close` (10 sessions elapsed).
* **Outcome:** Gross P&L $+367.8\text{ bps}$, Net of 25 bps RT: **`+342.8 bps` (Win)**.

#### D. Steel Historical Replay (Example: `CLF` Throughput Surge)
* **Signal Observation Date ($t$):** `2023-11-06 Close`
* **Observation State:** `CLF` $\text{YoY} = 1.82 \ge Q_{80} = 1.41$ (Cloud $8.5\%$, Gate Unfiltered).
* **Order Entry ($t+1$):** `2023-11-07 Close`
* **Execution:** Long `CLF` @ $\$16.45$ ($10.0\%$ notional), Short `XME` hedge @ $\$51.20$ ($\beta = 1.35$).
* **Exit Date:** `2023-11-21 Close` (10 sessions elapsed).
* **Outcome:** Gross P&L $+543.2\text{ bps}$, Net of 25 bps RT: **`+518.2 bps` (Win)**.

---

## 5. Slide-Ready Artifacts

1. **Figure:** [fig_dual_sleeve_paper_replay.png](file:///Users/siddharthgupta/Downloads/Documents/ALPHA/osint-alpha-pipeline/audit_evidence_pack/fig_dual_sleeve_paper_replay.png) (300 DPI, 16:9 institutional equity and drawdown profiles).
2. **Replay Curves Data:** [portfolio_equity_drawdown.parquet](file:///Users/siddharthgupta/Downloads/Documents/ALPHA/osint-alpha-pipeline/audit_evidence_pack/portfolio_equity_drawdown.parquet).
3. **Summary JSON:** [combined_replay_metrics.json](file:///Users/siddharthgupta/Downloads/Documents/ALPHA/osint-alpha-pipeline/audit_evidence_pack/combined_replay_metrics.json).
4. **2x2 Steel Matrix Results:** [steel_2x2_results.json](file:///Users/siddharthgupta/Downloads/Documents/ALPHA/osint-alpha-pipeline/audit_evidence_pack/steel_2x2_results.json).
