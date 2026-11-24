"""
Generates isolated GeoJSON files for the Steel 2x2 Audit:
1. audit_evidence_pack/facilities_steel_original.geojson (exact copy of current coordinates in config/)
2. audit_evidence_pack/facilities_steel_verified.geojson (verified plant coordinates based on physical satellite ground truth)
"""

import json
from pathlib import Path

FACILITIES_SRC = Path("config/facilities_industrial.geojson")
AUDIT_DIR = Path("audit_evidence_pack")

# Verified plant coordinates (longitude, latitude) based on satellite imagery & addresses
VERIFIED_COORDS = {
    # Cleveland-Cliffs (CLF)
    "clf_indiana_harbor": [-87.4280, 41.6640],     # East Chicago, IN (blast furnaces / BOF complex)
    "clf_burns_harbor": [-87.1380, 41.6370],       # Burns Harbor, IN (verified mill core)
    "clf_middletown": [-84.3830, 39.4780],         # Middletown Works, OH (BOF / caster core)
    
    # Nucor (NUE)
    "nue_berkeley": [-79.8860, 33.0140],           # 1455 Hagan Ave, Huger, SC (EAF & caster mill)
    "nue_crawfordsville": [-86.8380, 39.9760],     # 4537 S Nucor Rd, Crawfordsville, IN (pioneering CSP thin-slab mill)
    "nue_hickman": [-89.7140, 35.9360],            # 7301 E County Rd 142, Blytheville, AR (Hickman flat-rolled mill)
    
    # Steel Dynamics (STLD)
    "stld_butler": [-84.9210, 41.3670],            # 4500 County Road 59, Butler, IN (flat roll division)
    "stld_columbus": [-88.5710, 33.4410],          # 1945 Airport Rd, Columbus, MS (Golden Triangle flat roll mill)
    
    # Commercial Metals Co (CMC)
    "cmc_birmingham": [-86.7810, 33.5830],         # 2301 Huntsville Rd, Birmingham, AL (rebar / merchant mill)
    "cmc_cayce": [-81.0510, 33.9630],              # Cayce, SC (Columbia mill)
    "cmc_durant": [-96.3490, 33.9820],             # 2240 Old US Hwy 70, Durant, OK (micro-mill)
    "cmc_seguin": [-98.0340, 29.5760],             # 1 Steel Mill Dr, Seguin, TX (steel mini-mill)
}

def main():
    with open(FACILITIES_SRC, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Filter strictly to STEEL sector
    steel_orig_features = [feat for feat in data["features"] if feat["properties"].get("sector") == "STEEL"]
    orig_fc = {"type": "FeatureCollection", "features": steel_orig_features}
    
    orig_path = AUDIT_DIR / "facilities_steel_original.geojson"
    with open(orig_path, "w", encoding="utf-8") as f:
        json.dump(orig_fc, f, indent=2)
    print(f"Saved {len(steel_orig_features)} original steel facilities to {orig_path}")

    # Build verified feature collection
    verified_features = []
    for feat in steel_orig_features:
        new_feat = json.loads(json.dumps(feat))
        fid = new_feat["properties"]["facility_id"]
        if fid in VERIFIED_COORDS:
            new_feat["geometry"]["coordinates"] = VERIFIED_COORDS[fid]
            new_feat["properties"]["verified"] = True
        verified_features.append(new_feat)

    ver_fc = {"type": "FeatureCollection", "features": verified_features}
    ver_path = AUDIT_DIR / "facilities_steel_verified.geojson"
    with open(ver_path, "w", encoding="utf-8") as f:
        json.dump(ver_fc, f, indent=2)
    print(f"Saved {len(verified_features)} verified steel facilities to {ver_path}")

if __name__ == "__main__":
    main()
