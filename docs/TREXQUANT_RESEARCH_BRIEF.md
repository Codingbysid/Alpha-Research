# Trexquant Research Brief: Satellite Thermal Anomaly Alpha

**Strategy Name:** `OSINT-SHORT-Q5` (Idiosyncratic Refinery Distress via VIIRS Thermal Flares)  
**Target Asset Class:** U.S. Downstream Energy Equities (`VLO`, `MPC`, `PSX`, `DINO`, `PBF`, `DK`, `CVI`)  
**Benchmark / Hedge Leg:** Energy Select Sector SPDR Fund (`XLE`)  
**Execution Style:** Daily Rebalanced, Beta-Neutral, 10-Day Holding Horizon  
**Repository:** [Codingbysid/Alpha-Research](https://github.com/Codingbysid/Alpha-Research)  

---

## 1. Academic Paper Stack

### Primary Anchor Paper
* **Mukherjee, A., Panayotov, G., & Shon, J. (2021).** *"Eye in the Sky: Private Satellites and Government Macro Data."* **Journal of Financial Economics**, 141(1), 234–254.  
* **Paper Link:** [https://ideas.repec.org/a/eee/jfinec/v141y2021i1p234-254.html](https://ideas.repec.org/a/eee/jfinec/v141y2021i1p234-254.html)  
* **Why It Anchors This Research:**  
  Mukherjee et al. directly investigate satellite optical/infrared observation of U.S. oil infrastructure (floating-roof crude storage tanks) alongside official government reports (EIA Weekly Petroleum Status Report). Crucially, the authors identify **cloud cover attenuation** as a major source of measurement noise in satellite telemetry that institutional investors must model.
* **Our Extension & Empirical Discovery:**  
  Mukherjee et al. hypothesize that satellite surveillance generates alpha by *front-running aggregate macro announcements* (EIA national inventories). We explicitly tested this hypothesis on nationwide refinery utilization: we aggregated national capacity-weighted thermal flaring ($\text{us\_yoy\_mean}$) against the Wednesday EIA weekly refinery run prints.  
  **Finding:** The macro relationship fails ($\text{IC} \approx 0$). Satellite observations do not predict aggregate EIA macro surprises because refiners stagger maintenance and planned turnarounds. Instead, we demonstrated that **thermal satellite alpha lives entirely in idiosyncratic, single-stock operational distress**: an individual refinery flaring excessively during clear-sky conditions signals emergency unit tripping (e.g., fluid catalytic cracker or coker upsets), yielding a robust negative cross-sectional return against its beta-hedged peer group (Short Q5 vs. $\beta \cdot \text{XLE}$).

### Supporting Papers
* **Elvidge, C. D., Zhizhin, M., Baugh, K., Hsu, F.-C., & Ghosh, T. (2016).** *"Methods for Global Survey of Natural Gas Flaring from Visible Infrared Imaging Radiometer Suite Data."* **Energies**, 9(1), 14.  
* **DOI:** [https://doi.org/10.3390/en9010014](https://doi.org/10.3390/en9010014)  
* **Role in Deck:** Establishes the physical remote-sensing foundation. Explains why VIIRS Nightfire (VNF) multispectral radiometry in the short-wave and mid-wave infrared bands (M7, M8, M10, M12, M13) accurately isolates high-temperature combustion sources (1,200–1,800 K) from ambient surface heat. Industrial gas flaring at petroleum refineries represents approximately 8% of total global flaring volume.
* **Zhu, C. (2019).** *"Big Data as a Governance Mechanism."* **The Review of Financial Studies**, 32(5), 2021–2061.  
* **DOI:** [https://doi.org/10.1093/rfs/hhy081](https://doi.org/10.1093/rfs/hhy081)  
* **Role in Deck:** Provides the financial and asset-pricing mechanism. Demonstrates that alternative data and real-time physical monitoring reduce corporate information asymmetry. By observing operational outages in real time, quantitative strategies price operational distress days before corporate press releases, state environmental filings (e.g., Texas TCEQ emission reports), or 10-Q earnings calls.

---

## 2. Formal Mathematical Alpha Formulation

The strategy operates as a **two-step predictive model**: first deriving a point-in-time, per-pass, capacity-weighted thermal anomaly $S_{i,t}$, followed by a regime- and macro-gated beta-hedged short allocation $\alpha_{i,t}$.

### Step A: Capacity-Weighted Per-Pass Thermal Anomaly

For refiner $i$ owning facilities $f \in \mathcal{F}_i$, with capacity weights $w_f = \frac{\text{Cap}_f}{\sum_{k \in \mathcal{F}_i} \text{Cap}_k}$, and daily clear-sky masked per-pass Fire Radiative Power (FRP):

$$F_{i,t} = \sum_{f \in \mathcal{F}_i} w_f \cdot \frac{\text{FRP}^{\text{night}}_{f,t}}{N^{\text{pass}}_{f,t}}$$

The normalized operational distress ratio is defined as:

$$S_{i,t} = \frac{\frac{1}{5}\sum_{k=0}^{4} F_{i,t-k}}{\frac{1}{21}\sum_{k=0}^{20} F_{i,t-252-k}}$$

### Step B: Gated Alpha Signal (10-Day Holding Horizon)

$$\alpha_{i,t} = -\,\mathbb{I}\!\left(S_{i,t} \ge Q_{0.80}(\mathcal{H}_{t-10})\right) \cdot \mathbb{I}\!\left(C_{i,t} \le 40\%\right) \cdot \mathbb{I}\!\left(Z^{\text{crack}}_{60,t} \le 1.0\right) \cdot \mathbb{I}\!\left(R_t = \text{Idiosyncratic}\right)$$

### Execution & Sizing: Beta-Neutral Pair Portfolio
Each active signal enters as a beta-neutral pair:
$$\text{Short } 1\text{ unit of Refiner } i \quad / \quad \text{Long } \beta_{i,t}^{(60)} \text{ units of XLE}$$

Where position weight $w_{i,t}$ is sized under a **Merton Jump-Diffusion 95% CVaR** risk budget:
$$w_{i,t} = \min\!\left(\frac{0.015}{\text{CVaR}_{0.95,i,t}},\, 0.10\right)$$

Subject to book-level leverage caps: $\sum_{i} |w_{i,t}| \le 0.60$.

---

### Variable Definitions & Economic Intuition

| Term | Mathematical Definition | Economic / Remote Sensing / Statistical Intuition |
| :--- | :--- | :--- |
| $N^{\text{pass}}_{f,t}$ | Unique VIIRS satellite overpasses on day $t$ | **Eliminates constellation drift.** Normalizes detection count across Suomi-NPP, NOAA-20, and NOAA-21 (launched March 2023). Prevents false flare spikes caused by increased satellite sampling frequency. |
| $S_{i,t}$ | 5-day vs. prior-year 21-day per-pass FRP ratio | **Controls for seasonal maintenance.** Compares short-term emergency flaring against the facility's baseline during the same seasonal turnaround window 252 trading days prior. |
| $Q_{0.80}(\mathcal{H}_{t-10})$ | Causal 80th percentile with 10-day embargo | **Strict point-in-time thresholding.** Eliminates forward-looking bias by calculating quantiles only on historical returns that have fully resolved their 10-day forward holding horizon. |
| $C_{i,t} \le 40\%$ | Capacity-weighted Open-Meteo cloud cover | **Mukherjee et al. cloud-masking effect.** Eliminates false zeros caused by dense cloud cover absorbing thermal infrared emissions. |
| $Z^{\text{crack}}_{60,t} \le 1.0$ | 60-day $z$-score of Gulf Coast 3-2-1 crack spread | **Margin breakout hedge.** Prevents shorting refiners during macro refining margin squeezes when high margins overpower operational downtime. Lifts Sharpe from `0.28` to `0.39`. |
| $R_t$ | 2-State HMM on OVX & Market Volatility | **Macro panic filter.** Avoids initiating idiosyncratic operational shorts during systemic oil crises ($\text{OVX} \ge 45$) when commodity beta completely dominates micro alpha. |

---

## 3. Empirical Scorecard & Research Milestones

```
+---------------------------------------------------------------------------------------------------+
|                                OSINT REFINERY ALPHA SCORECARD                                     |
+------------------------------------+--------------------------+-----------------------------------+
| Metric / Experiment                | Baseline / Naive         | Engineered / Gated Alpha          |
+------------------------------------+--------------------------+-----------------------------------+
| NOAA-21 Overpass Normalization      | Raw FRP Sum (drift +40%) | Per-Pass Normalized               |
| Non-Overlapping 10-Day Rank IC     | -0.0410 (insignificant)  | -0.1751 (t = -2.44, p = 0.0157)   |
| Out-of-Sample Transfer (4 Tickers) | In-Sample (VLO,MPC,PSX)  | OOS (DINO,PBF,DK,CVI): +143.9 bps |
| 3-2-1 Crack Spread Gate (z <= 1.0) | Unconstrained (SR 0.28)  | Crack-Gated: Sharpe 0.39, PF 1.62 |
| Calendar Year / Sub-period Hit Rate| 7 / 10 Books Positive    | 10 / 10 Books Positive (100%)     |
| Maximum Strategy Drawdown          | 8.12% (unhedged macro)   | 3.66% (beta-hedged + CVaR sized)  |
| Machine Learning vs. Domain Physics| HistGradientBoosting+MLP | Parsimonious Physical Rule        |
| Walk-Forward ML Sharpe Ratio       | Sharpe 0.12 (Overfitted) | Sharpe 0.39 (Robust Physics)      |
| Cross-Sector Expansion (Steel)     | Assumed Negative IC      | Positive IC (+0.1434, Sign Flip!) |
| AIS Vessel Dwell Confirmation      | Satellite Flaring Only   | Fused AIS: High Dwell (-677 bps)  |
+------------------------------------+--------------------------+-----------------------------------+
```

### Milestone Highlights:
1. **NOAA-21 Constellation Normalization ($p = 0.0157$):**  
   In March 2023, NOAA-21 became operational, adding a 3rd VIIRS sensor alongside Suomi-NPP and NOAA-20. Unadjusted raw FRP jumped ~40%, causing severe threshold distortion. Normalizing by unique overpass count ($N^{\text{pass}}_{f,t}$) restored stationary telemetry and established a non-overlapping 10-day Rank IC of **$-0.1751$ ($p = 0.0157$)**.
2. **Out-of-Sample Geographic Generalization ($+143.9\text{ bps}$):**  
   Calibrated on 3 Gulf Coast refiners (`VLO`, `MPC`, `PSX`) and tested out-of-sample on 4 mid-continent and regional operators (`DINO`, `PBF`, `DK`, `CVI`). The Short Q5 strategy generated **$+143.9\text{ bps}$** annualized excess return on the out-of-sample set with identical thresholds, proving zero curve-fitting to specific facilities.
3. **Macro Crack Spread Gate ($10/10\text{ Positive Books}$):**  
   During structural margin expansions (e.g., 2022 diesel shortages post-Ukraine invasion), crack spreads exploded higher, allowing refiners to remain highly profitable despite partial flaring. Gating shorts when 60-day crack spread $z$-score $> 1.0$ eliminated short drawdowns, raising Sharpe from **$0.28 \rightarrow 0.39$**, lowering Max Drawdown to **$3.66\%$**, and delivering **$10$ out of $10$ positive calendar sub-period books**.
4. **Parsimonious Domain Physics Beats ML Trees ($0.39\text{ vs. } 0.12\text{ Sharpe}$):**  
   Benchmarked an expanding walk-forward purged machine learning model (`HistGradientBoostingRegressor` + `Ridge` blend across 11 features with 10-day embargo). The ML model achieved an OOS Sharpe of only **$0.12$**, while the simple physical threshold rule achieved **$0.39$**. In thin cross-sections with alternative physical data, low-parameter domain physics dominates complex multi-feature models.
5. **Physical Process Grounding: Petrochemicals vs. Steel Sign Flip ($+0.1434$ IC):**  
   - *Petrochemicals (`LYB`, `DOW`, `CF`):* Flaring indicates chemical trip events and emergency pressure relief, confirming negative IC like refiners.  
   - *Steel Manufacturing (`CLF`, `NUE`, `STLD`):* Blast furnaces and electric arc furnaces emit intense thermal heat *while actively manufacturing*. Increased thermal output correlates with peak production, resulting in a **positive IC of $+0.1434$**.  
   *Takeaway for Quants:* Blindly pooling industrial tickers without understanding plant thermodynamics cancels out alpha.
6. **Maritime AIS Tanker Fusion (Physical Throughput Confirmation):**  
   Ingested 30M+ NOAA MarineCadastre and AISStream vessel records for Gulf Coast ports. Tracking tanker dwell times at terminal berths revealed that facilities with high clear-sky thermal flares accompanied by vessel berthing bottlenecks (>48h dwell) underperformed non-congested peers by **$-677\text{ bps}$** over the subsequent 10 trading days.

---

## 4. Production System & Automated Paper Broker

The production implementation is designed for autonomous enterprise deployment:
* **Cloud Architecture:** Fully automated AWS Lambda execution (`osint-short-q5-daily`) packaged as an ARM64 container on Amazon ECR.
* **Storage & Ledgers:** Persistent state, market features, HMM regimes, and execution ledgers stored in Amazon S3 (`s3://osint-alpha-pipeline-001676203103/osint-alpha/data/`).
* **Broker Execution:** Integrated with **Alpaca Paper Trading API** (`alpaca-py`) supporting:
  - Deterministic client order IDs (`{trade_id}_SHORT`, `{trade_id}_CLOSE_SHORT`, `{trade_id}_HEDGE_BUY`, `{trade_id}_HEDGE_SELL`) to guarantee idempotency.
  - Pre-trade asset shortability checks (`asset.tradable and asset.shortable`).
  - Strict per-ticker `APIError` isolation preventing rejected orders from skewing aggregate `XLE` hedge sizing.
  - Automated pre-trade broker reconciliation comparing signed live Alpaca shares against open ledger positions.
  - Daily blotter telemetry with webhook alerts on drift or order rejections.
