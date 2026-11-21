# Trexquant Research Presentation Brief: Thermal Satellite OSINT Alpha in U.S. Refining

**Author:** Siddharth Gupta  
**Target Date:** December 14, 2026 (20-minute presentation + 10-minute Q&A)  
**Core Thesis:** Nighttime satellite thermal flaring spikes at U.S. oil refineries do *not* predict macro government utilization reports (EIA), but they strongly predict **idiosyncratic, single-stock operational distress**—delivering a statistically significant, beta-hedged 10-day short alpha that validates out-of-sample across unseen refiners and inverts sign when applied to U.S. steel mills.

---

## 1. Academic Literature Foundation

### Primary Anchor Paper
* **Mukherjee, A., Panayotov, G., & Shon, J. (2021).** *"Eye in the Sky: Private Satellites and Government Macro Data."* **Journal of Financial Economics**, 141(1), 234–254.  
  * **Link:** https://ideas.repec.org/a/eee/jfinec/v141y2021i1p234-254.html
  * **Key Idea:** Satellite imagery of U.S. crude oil storage (Cushing, OK) provides an early read on government macro releases (EIA) and improves price discovery, with **cloud cover** acting as a critical observation-quality filter.
  * **Our Extension:** We test Mukherjee et al.'s macro front-running hypothesis on U.S. refinery thermal emissions vs. Wednesday 10:30 AM ET EIA utilization releases, prove it **fails at the macro level** ($\text{IC} \approx 0$), and pivot the satellite signal to **cross-sectional, single-stock operational upsets** (Short stock / Long $\beta \cdot \text{XLE}$).

### Supporting Papers
* **Elvidge, C. D., Zhizhin, M., Baugh, K., Hsu, F.-C., & Ghosh, T. (2016).** *"Methods for Global Survey of Natural Gas Flaring from Visible Infrared Imaging Radiometer Suite Data."* **Energies**, 9(1), 14.  
  * **DOI:** https://doi.org/10.3390/en9010014
  * **Role:** Establishes the remote-sensing physics—nighttime NASA/NOAA VIIRS infrared Fire Radiative Power (FRP) isolates industrial gas flaring at refineries.
* **Zhu, C. (2019).** *"Big Data as a Governance Mechanism."* **The Review of Financial Studies**, 32(5), 2021–2061.  
  * **DOI:** https://doi.org/10.1093/rfs/hhy081
  * **Role:** Supplies the asset-pricing mechanism—alternative data accelerates price discovery by surfacing real-time operational shocks before management disclosures or quarterly earnings.

---

## 2. Formal Alpha Equation

### Step A: Capacity-Weighted Per-Pass Thermal Anomaly
For refiner $i$ with facilities $f \in \mathcal{F}_i$, capacity weights $w_f = \frac{\text{Cap}_f}{\sum_{k \in \mathcal{F}_i} \text{Cap}_k}$, and daily clear-sky masked per-pass Fire Radiative Power $F_{i,t} = \sum_{f \in \mathcal{F}_i} w_f \cdot \frac{\text{FRP}^{\text{night}}_{f,t}}{N^{\text{pass}}_{f,t}}$:

$$S_{i,t} = \frac{\frac{1}{5}\sum_{k=0}^{4} F_{i,t-k}}{\frac{1}{21}\sum_{k=0}^{20} F_{i,t-252-k}}$$

### Step B: Gated Alpha Signal (10-Trading-Day Horizon)
$$\alpha_{i,t} = -\,\mathbb{I}\!\left(S_{i,t} \ge Q_{0.80}(\mathcal{H}_{t-10})\right) \cdot \mathbb{I}\!\left(C_{i,t} \le 40\%\right) \cdot \mathbb{I}\!\left(Z^{\text{crack}}_{60,t} \le 1.0\right) \cdot \mathbb{I}\!\left(R_t = \text{Idiosyncratic}\right)$$

* **Execution Structure:** Beta-neutral pair — Short $1$ unit of refiner $i$ / Long $\beta_{i,t}^{(60)}$ units of `XLE`.
* **Risk Budgeting:** Merton Jump-Diffusion 95% CVaR cap ($1.5\%$ equity limit per trade):
  $$w_{i,t} = \min\!\left(\frac{0.015}{\text{CVaR}_{0.95,i,t}},\, 0.10\right)$$

### Variable & Gate Definitions
| Term | Definition | Economic / Statistical Intuition |
| :--- | :--- | :--- |
| $N^{\text{pass}}_{f,t}$ | Unique VIIRS satellite overpasses on day $t$ | Eliminates the March 2023 NOAA-21 3rd-satellite constellation count jump. |
| $S_{i,t}$ | 5-day vs. prior-year 21-day per-pass FRP ratio | Controls for seasonal turnaround cycles; spikes signal abnormal emergency flaring. |
| $Q_{0.80}(\mathcal{H}_{t-10})$ | Causal 80th percentile with 10-day embargo | Prevents look-ahead leakage from unresolved 10-day forward return labels. |
| $C_{i,t} \le 40\%$ | Capacity-weighted Open-Meteo cloud cover | Removes false-zero thermal attenuation (Mukherjee et al. cloud-mask effect). |
| $Z^{\text{crack}}_{60,t} \le 1.0$ | 60-day $z$-score of 3-2-1 crack spread | Blocks shorts during macro margin breakouts where sector rallies swamp outages. |
| $R_t$ | 2-state causal HMM + $\text{OVX} \ge 45.0$ floor | Stands aside in macro oil panics (2020 COVID, 2022 Ukraine) without false-flagging calm years. |

---

## 3. Data Universe & Pipeline Summary (2018–2026)

* **Satellite Thermal Data:** NASA FIRMS VIIRS Archives (Suomi-NPP, NOAA-20, NOAA-21) — **138,540** deduplicated refinery-corridor detections across **22 CONUS facilities**.
* **Cloud Mask:** Open-Meteo historical hourly cloud cover matched to each facility coordinate.
* **Equity Universe (7 Pure-Play CONUS Refiners, 15,645 daily rows):**
  * **In-Sample Discovery Set (3 tickers, 9 facilities):** `VLO`, `MPC`, `PSX`
  * **Out-of-Sample Validation Set (4 tickers, 13 facilities):** `DINO`, `PBF`, `DK`, `CVI` (continuous 2018–2026 ownership to avoid M&A look-ahead bias).
* **Macro & Maritime Data:** NYMEX 3-2-1 Crack Spread (`CL=F`, `RB=F`, `HO=F`), EIA API v2 Weekly Petroleum Status Reports (strict PIT `release_date = friday + 5d`), and NOAA MarineCadastre Gulf Coast AIS (2.45M pings, 788 tankers, June 2026).

---

## 4. Empirical Audit & Research Discoveries

### Discovery 1: Catching the NOAA-21 Constellation Artifact & Overlapping $t$-Stat Inflation
Raw summed FRP inflated `frp_yoy_ratio` for 12 months after NOAA-21 launched in March 2023. Normalizing by overpass count (`yoy_per_pass`) and testing on **strictly non-overlapping 10-day blocks** (every 10th trading day) confirms the alpha survives:

| Feature (Clear Sky $\le 40\%$, 10d Excess) | $n$ (Non-Overlapping) | Spearman IC | $t$-stat | $p$-value |
| :--- | :---: | :---: | :---: | :---: |
| `frp_yoy_ratio` (Raw Sum — biased by NOAA-21) | 187 | `-0.2210` | `-3.08` | `0.0024` |
| **`yoy_per_pass` (Per-Pass Normalized)** | **190** | **`-0.1751`** | **`-2.44`** | **`0.0157`** |
| `log_pp_z90` (90-day level $z$-score) | 236 | `+0.0262` | `+0.40` | `0.6893` |

---

### Discovery 2: Strict Out-of-Sample Validation on 4 Unseen Refiners
Expanding from 3 discovery tickers (`VLO`, `MPC`, `PSX`) to 4 unseen tickers (`DINO`, `PBF`, `DK`, `CVI`) **expanded** the Short Q5 edge (10-day forward excess return, clear sky + idiosyncratic regime):

| Split | Q5 Mean 10d Fwd Excess | Q2–Q3 vs. Q5 Gross Spread | Short Q5 Net Avg (15 bps RT) | Short Q5 Win Rate | Short Q5 $n$ |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **In-Sample (`VLO`, `MPC`, `PSX`)** | `-0.830%` | `+113 bps` | `+70.3 bps` | `59.3%` | 236 |
| **Out-of-Sample (`DINO`, `PBF`, `DK`, `CVI`)** | **`-1.504%`** | **`+138 bps`** | **`+143.9 bps`** | **`61.0%`** | **484** |
| **Full Universe (All 7 Refiners)** | `-1.146%` | `+129 bps` | `+87.8 bps` | `58.7%` | 741 |

*Key Insight:* The Long Q2–Q3 leg was dead weight (`-26.9 bps` OOS), proving that **Pure Beta-Hedged Short Q5 (Short stock / Long $\beta \cdot \text{XLE}$)** is the clean expression of the alpha.

---

### Discovery 3: HMM Regime Calibration (Fixing 2019 False Positives)
A naive 2-state Gaussian HMM fit on calm 2018 flagged **71.9% of 2019** as `MACRO_PANIC` (avg OVX = 33.1). Adding an absolute **$\text{OVX} \ge 45.0$ floor** eliminated calm-year false positives while preserving true macro shock detection:

| Stress / Calm Window | 2-State HMM Only (% Panic) | 2-State HMM + $\text{OVX} \ge 45$ (% Panic) |
| :--- | :---: | :---: |
| **2019 Calm Baseline** (`2019-01-01..2019-12-31`) | `71.9%` | **`2.0%`** |
| **2020 COVID Oil Shock** (`2020-02-15..2020-05-15`) | `100.0%` | **`88.9%`** |
| **2022 Ukraine Energy Spike** (`2022-02-01..2022-07-31`) | `88.7%` | **`86.3%`** |

---

### Discovery 4: 3-2-1 Crack Spread Gate, Execution Lag, & EIA Macro Refutation
Evaluated across **10 staggered non-overlapping sub-books (offsets 0–9)**, strictly embargoed, net of **25 bps round-trip costs** (short borrow + `XLE` hedge slippage):

| Configuration (10 Staggered Books, Net 25 bps RT) | Avg $N$ / Book | Mean Net Ret | Ex-Best Ret | Sharpe | Profit Factor | Max DD | Win Rate | Positive Books |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Baseline: Pure Short Q5 (MOC) | 73.7 | `+6.04%` | `+3.32%` | `0.28` | `1.39` | `5.58%` | `59.0%` | `9 / 10` |
| Baseline: Pure Short Q5 (1-Day Lag $T+1$) | 73.7 | `+4.70%` | `+1.99%` | `0.24` | `1.33` | `5.61%` | — | `9 / 10` |
| **Production: Short Q5 + `crack_z60 <= +1.0`** | **56.4** | **`+6.75%`** | **`+4.06%`** | **`0.39`** | **`1.62`** | **`3.66%`** | **`59.0%`** | **`10 / 10`** |
| Diagnostic: `crack_z60 > +1.0` ONLY | 17.3 | `-0.67%` | `-1.94%` | `-0.01` | `1.17` | `3.89%` | `59.0%` | `6 / 10` |
| Purged Walk-Forward ML (`Ridge + HistGBDT`) | 85.4 | `+2.28%` | `+0.51%` | `0.12` | `1.15` | `6.70%` | `55.0%` | `7 / 10` |

* **Ticker Breadth (Baseline MOC, Net 25 bps):** **6 / 7 tickers positive** (`CVI` +139.4 bps, `PSX` +132.9 bps, `VLO` +101.6 bps, `DINO` +97.7 bps, `DK` +61.5 bps, `MPC` +23.8 bps, `PBF` -88.6 bps).
* **2022 Turnaround:** Under `crack_z60 <= 1.0`, 2022 flipped from `-51.7 bps` to **`+358.3 bps` (77.3% WR)** by standing aside during Ukraine-war margin explosions.
* **Why Simple Physics Beat ML:** On a thin 7-ticker cross-section, a 11-feature `Ridge + HistGradientBoosting` ensemble degraded OOS non-overlapping IC to `+0.0288` ($p = 0.5038$) and cut Sharpe from `0.39` to `0.12`.
* **EIA Macro Front-Run Refutation ($n = 364$ weeks):** Aggregating Friday NASA thermal signals against Wednesday 10:30 AM ET EIA utilization yielded $\text{IC} = -0.0094$ ($t = -0.18, p = 0.8585$) for US utilization WoW and $\text{IC} = -0.0191$ ($t = -0.36, p = 0.7160$) for PADD 3—proving thermal flaring is an **idiosyncratic equity signal**, not a macro utilization proxy.
* **Cost Sensitivity & Uninflated Block-Bootstrap (10,000 Draws per Book):**
  | Execution / Borrow Cost Tier | Mean Net Ret | Sharpe | Profit Factor | Max DD | Positive Books | Med Boot $P(\text{Sharpe} \le 0)$ | 95% Confidence Interval |
  | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
  | **15 bps RT** | `+7.06%` | `0.40` | `1.65` | `3.60%` | `10 / 10` | **`0.154`** | `[-0.02%, +0.10%]` |
  | **25 bps RT (Baseline)** | `+6.45%` | `0.37` | `1.59` | `3.66%` | `9 / 10` | **`0.176`** | `[-0.03%, +0.09%]` |
  | **50 bps RT (Severe HTB)** | `+4.95%` | `0.29` | `1.44` | `3.85%` | `9 / 10` | **`0.227`** | `[-0.03%, +0.08%]` |
  | **75 bps RT (Extreme Stress)** | `+3.47%` | `0.21` | `1.30` | `4.06%` | `9 / 10` | **`0.283`** | `[-0.04%, +0.08%]` |

---

### Discovery 5: Cross-Sector Physics Validation (Refining vs. Steel vs. Petrochemicals)
Applying the exact same per-pass VIIRS pipeline to **15 industrial facilities** (2018–2026) confirmed the underlying physical mechanism:

| Sector & Hedge ETF | Tickers | Non-Overlapping 10d IC | $t$-stat | $p$-value | Physical Mechanism |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **US Refining (`XLE`)** | `VLO`, `MPC`, `PSX`, `DINO`, `PBF`, `DK`, `CVI` | **`-0.1751`** | **`-2.44`** | **`0.0157`** | Waste flaring / unit trips (**Short** signal) |
| **US Steel (`XME`)** | `CLF`, `NUE`, `STLD` | **`+0.1434`** | **`+1.79`** | **`0.0751`** | Blast furnace / EAF throughput (**Long** signal) |
| **US Petrochem (`XLB`)** | `LYB`, `DOW`, `CF` | `+0.0745` | `+0.97` | `0.3344` | Noise — global feedstock spreads dominate |

* **Steel Long Q5 10-Book Performance & Uninflated Bootstrap (Net 25 bps RT):**
  | Strategy Variant | Avg $N$ / Book | Mean Net Ret | Sharpe | Profit Factor | Max DD | Positive Books | Med Boot $P(\text{Sharpe} \le 0)$ | 95% Confidence Interval |
  | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
  | **Long Q5 Steel (All)** | 30.7 | `+2.29%` | `+0.20` | `1.48` | `2.75%` | `7 / 10` | **`0.181`** | `[-0.01%, +0.04%]` |
  | **Long Q5 Steel + XME 20d Mom > 0** | 18.1 | `+1.23%` | `+0.12` | `1.59` | `1.84%` | `7 / 10` | **`0.392`** | `[-0.01%, +0.03%]` |

---

### Discovery 6: Maritime AIS Sensor Fusion (June 2026 Gulf Coast Pilot)
To separate **unplanned upset flaring** from **high run-rate flaring**, we fused NASA VIIRS with **2,449,874 NOAA MarineCadastre AIS pings (788 tankers)** across Port Arthur, Houston/Galveston, and Freeport:

* **Tanker Queue Congestion (`tanker_z60`):** $\text{IC} = -0.5302$ ($t = -6.19, p < 0.0001, n = 100$)
* **Slow Dwell Share (`< 1.5 knots`):** $\text{IC} = -0.4916$ ($t = -5.59, p < 0.0001, n = 100$)
* **Mean Laden Draught (meters):** $\text{IC} = +0.4206$ ($t = +4.59, p < 0.0001, n = 100$)
* **2x2 Confirmation Matrix:** During thermal spikes, **High Dwell ($>75\%$, bottleneck)** underperformed **Low Dwell (fluid dock flow)** by **`-677 bps`** over 10 days (`+7.34%` vs. `+14.11%` during a macro rally).

---

## 5. Limitations, Live Deployment & Further Research

* **Known Limitations:** Thin cross-section (7 refiners) limits standalone Sharpe to `0.39` with 3 of 8 years still negative (2019, 2023, 2025); cloud cover masks ~50% of daily observations; AIS fusion is currently validated on a 1-month pilot (June 2026).
* **Live Production Validation (1-Slide Mention):** Deployed as a containerized **AWS Lambda (Graviton) + S3 + EventBridge Scheduler** pipeline running weekdays at 15:50 ET, connected to **Alpaca Paper Trading** with aggregated net `XLE` hedge rebalancing to track real forward slippage and borrow availability.
* **Next Research Steps:** (1) Expand NOAA MarineCadastre AIS validation across 12–24 months to promote tanker dwell/draught into a live confirmation gate; (2) Condition the positive-IC **US Steel Long Q5** sleeve on `XME` momentum to build a market-neutral multi-sector physical OSINT portfolio.
