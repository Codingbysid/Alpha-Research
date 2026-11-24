"""
Downloads and caches daily adjusted close prices for all 11 portfolio assets + 2 ETFs:
- Refiners: VLO, MPC, PSX, DINO, PBF, DK, CVI
- Energy ETF: XLE
- Steel: CLF, NUE, STLD, CMC
- Metals ETF: XME
- Benchmark: SPY
Saves to audit_evidence_pack/market_prices.parquet.
"""

from pathlib import Path
import pandas as pd
import yfinance as yf

AUDIT_DIR = Path("audit_evidence_pack")
CACHE_FILE = AUDIT_DIR / "market_prices.parquet"

SYMBOLS = [
    # Refiners (7)
    "VLO", "MPC", "PSX", "DINO", "PBF", "DK", "CVI",
    # Energy ETF
    "XLE",
    # Steel (4)
    "CLF", "NUE", "STLD", "CMC",
    # Metals ETF
    "XME",
    # Broad Market
    "SPY",
]

def fetch_and_cache_market_data():
    if CACHE_FILE.exists():
        print(f"Loading cached prices from {CACHE_FILE}...")
        return pd.read_parquet(CACHE_FILE)

    print(f"Downloading prices for {len(SYMBOLS)} symbols from Yahoo Finance (2017-06-01 to present)...")
    raw = yf.download(SYMBOLS, start="2017-06-01", auto_adjust=False, progress=False)
    field = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    px.to_parquet(CACHE_FILE)
    print(f"Saved {px.shape[0]} trading days x {px.shape[1]} symbols to {CACHE_FILE}")
    return px

if __name__ == "__main__":
    fetch_and_cache_market_data()
