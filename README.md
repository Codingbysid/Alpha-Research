# Alpha-Research: Satellite Thermal Anomaly Alpha

[![Alpha Research Brief](https://img.shields.io/badge/Research%20Brief-Trexquant%20Format-blue.svg)](docs/TREXQUANT_RESEARCH_BRIEF.md)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![AWS Lambda](https://img.shields.io/badge/AWS%20Lambda-Container%20ARM64-orange.svg)](https://aws.amazon.com/lambda/)
[![Alpaca Broker](https://img.shields.io/badge/Execution-Alpaca%20Paper%20Trading-brightgreen.svg)](https://alpaca.markets/)

A quantitative research pipeline and automated execution system exploiting **satellite infrared thermal flaring anomalies** at U.S. petroleum refineries to forecast idiosyncratic operational distress and trade beta-hedged short pairs against the Energy Select Sector SPDR Fund (`XLE`).

> **Comprehensive Research Brief:** For the full academic literature review, formal mathematical formulation, and empirical scorecard, see **[`docs/TREXQUANT_RESEARCH_BRIEF.md`](docs/TREXQUANT_RESEARCH_BRIEF.md)**.

---

## Strategy Overview

* **Asset Universe:** U.S. Downstream Refiners (`VLO`, `MPC`, `PSX`, `DINO`, `PBF`, `DK`, `CVI`).
* **Hedge Instrument:** Energy Sector ETF (`XLE`).
* **Core Physics:** Refineries flare high-temperature combustible gases during unplanned unit trip events (e.g., catalytic cracker upsets, power outages). NASA/NOAA VIIRS Nightfire satellite sensors detect these infrared thermal anomalies in real time before formal regulatory disclosures or earnings impact.
* **Per-Pass Constellation Normalization:** Normalizes Fire Radiative Power (FRP) by the unique daily satellite overpass count ($N^{\text{pass}}_{f,t}$), neutralizing the March 2023 NOAA-21 satellite count jump and establishing a non-overlapping 10-day Rank IC of **$-0.1751$ ($p = 0.0157$)**.
* **Macro Regime & Margin Gates:** Gated by 60-day crack spread $z$-scores ($Z^{\text{crack}}_{60,t} \le 1.0$) and a 2-state HMM volatility filter, elevating strategy Sharpe ratio to **$0.39$** (Profit Factor **$1.62$**, Max Drawdown **$3.66\%$**, **$10/10$** positive calendar sub-period books).
* **Physical AIS Tanker Confirmation:** Fuses satellite flaring with 30M+ NOAA/USCG maritime AIS records. Refiners experiencing clear-sky thermal flares accompanied by berth congestion (>48h tanker dwell) underperformed clean refiners by **$-677\text{ bps}$** over the next 10 trading days.

---

## Strategy Mathematical Model

### Step A: Capacity-Weighted Per-Pass Thermal Anomaly

$$F_{i,t} = \sum_{f \in \mathcal{F}_i} w_f \cdot \frac{\text{FRP}^{\text{night}}_{f,t}}{N^{\text{pass}}_{f,t}}$$

$$S_{i,t} = \frac{\frac{1}{5}\sum_{k=0}^{4} F_{i,t-k}}{\frac{1}{21}\sum_{k=0}^{20} F_{i,t-252-k}}$$

### Step B: Gated Alpha & Sizing

$$\alpha_{i,t} = -\,\mathbb{I}\!\left(S_{i,t} \ge Q_{0.80}(\mathcal{H}_{t-10})\right) \cdot \mathbb{I}\!\left(C_{i,t} \le 40\%\right) \cdot \mathbb{I}\!\left(Z^{\text{crack}}_{60,t} \le 1.0\right) \cdot \mathbb{I}\!\left(R_t = \text{Idiosyncratic}\right)$$

Position sizing uses a **Merton Jump-Diffusion 95% CVaR** risk budget:
$$w_{i,t} = \min\!\left(\frac{0.015}{\text{CVaR}_{0.95,i,t}},\, 0.10\right)$$

---

## Architecture & Codebase Layout

```
osint-alpha-pipeline/
├── config/
│   ├── facilities.geojson               # Refiner facility boundaries & BPD capacities
│   ├── facilities_industrial.geojson    # Steel & Petrochemical facility polygons
│   └── noaa_gulf_aoi.geojson            # Houston / Galveston / Sabine Pass maritime AOIs
├── docs/
│   └── TREXQUANT_RESEARCH_BRIEF.md      # Full academic brief, equation, & scorecard
├── scripts/
│   └── deploy_lambda.sh                 # Cloud deploy script (ECR push + Lambda config)
├── src/
│   ├── eval/                            # Backtest & statistical audit engines
│   │   ├── audit_ais_fusion.py          # AIS tanker dwell time & satellite fusion
│   │   ├── audit_ais_multimonth.py      # Multi-month AIS + VIIRS regime-split audit
│   │   ├── audit_crack_eia.py           # 3-2-1 crack spread & EIA macro correlation audit
│   │   ├── audit_industrial.py          # Cross-sector expansion (Steel vs. Petrochemicals)
│   │   ├── audit_short_q5.py            # Purged backtest with Merton CVaR sizing
│   │   ├── backtest_ls.py               # Long/short benchmark engine
│   │   └── ic_audit.py                  # Point-in-time Information Coefficient audit
│   ├── ingest/                          # Data acquisition layer
│   │   ├── ais_live.py                  # Live AISStream WebSocket client
│   │   ├── energy_macro.py              # EIA API crack spread & inventory builder
│   │   ├── firms_archive.py             # Historical NASA FIRMS VIIRS parser
│   │   ├── firms_live.py                # Daily NRT NASA FIRMS poller
│   │   └── market_data.py               # Refiner returns, OVX, & XLE market panel
│   ├── live/                            # Production execution layer
│   │   ├── alpaca_broker.py             # Idempotent Alpaca Paper Trading adapter
│   │   ├── daily_runner.py              # Daily mark-to-market, signal run, & blotter
│   │   ├── lambda_handler.py            # AWS Lambda entrypoint (S3 sync <-> execution)
│   │   └── smoke_test_alpaca.py         # Broker shortability & order idempotency test
│   ├── models/
│   │   └── train_ml.py                  # Purged walk-forward ML benchmark (HistGB + Ridge)
│   ├── risk/
│   │   ├── regime.py                    # 2-state HMM macro regime classification
│   │   └── sizing.py                    # Merton jump-diffusion CVaR calibrator
│   └── transform/
│       └── feature_engine.py            # Clear-sky filtering & feature construction
├── Dockerfile.lambda                    # AWS Lambda Python 3.12 container specification
└── .env.example                         # Environment configuration template
```

---

## Quickstart & Local Setup

### 1. Installation

```bash
git clone https://github.com/Codingbysid/Alpha-Research.git
cd Alpha-Research
python3 -m venv .venv
source .venv/bin/activate
pip install -r <(cat << 'EOF'
duckdb pandas geopandas shapely requests pyarrow python-dotenv
tzdata yfinance scipy hmmlearn scikit-learn boto3 alpaca-py
EOF
)
```

### 2. Configure Credentials

Copy `.env.example` to `.env` and fill in your API credentials:
```bash
cp .env.example .env
```

### 3. Run Daily Signal & Blotter (Dry-Run Mode)

```bash
PYTHONPATH=. python3 -W ignore src/live/daily_runner.py --skip-ingest --dry-run
```

Output:
```text
[broker] Connected to Alpaca Paper Trading. Account: PA360O6H5 | Live Equity: $100,000.00
=== OSINT SHORT-Q5 DAILY BLOTTER | AS OF 2026-11-20 (run 09:31 UTC) ===
Broker : ALPACA_PAPER | XLE Hedge Rebal: SKIP
Sync   : IN_SYNC (flat)
Errors : None
Regime : MACRO_PANIC (p_panic=1.000, OVX=54.12, reg_mult=0.00)
Crack  : $39.99/bbl (z60=-1.92, gate=PASS)
Equity : $100,000.00 (Realized: $+0.00 | Open MTM: $+0.00)
Model  : Q80=1.444 | Est p_win=0.560 | b=0.92 | Raw Kelly=+0.081
```

---

## Academic References

1. **Mukherjee, A., Panayotov, G., & Shon, J. (2021).** *"Eye in the Sky: Private Satellites and Government Macro Data."* **Journal of Financial Economics**, 141(1), 234–254.
2. **Elvidge, C. D., Zhizhin, M., Baugh, K., Hsu, F.-C., & Ghosh, T. (2016).** *"Methods for Global Survey of Natural Gas Flaring from Visible Infrared Imaging Radiometer Suite Data."* **Energies**, 9(1), 14.
3. **Zhu, C. (2019).** *"Big Data as a Governance Mechanism."* **The Review of Financial Studies**, 32(5), 2021–2061.
