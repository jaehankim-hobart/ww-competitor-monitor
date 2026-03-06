
# -*- coding: utf-8 -*-
from pathlib import Path
import pandas as pd

OUTPUT_DIR    = Path("output")
MANIFEST_CSV  = OUTPUT_DIR / "manifest.csv"                 # after ML
PIVOT_CSV     = OUTPUT_DIR / "report_by_product_line.csv"   # already created by rules.py, but we can regenerate

def build_pivot(manifest_csv=MANIFEST_CSV, pivot_csv=PIVOT_CSV):
    df = pd.read_csv(manifest_csv)
    pivot = pd.pivot_table(
        df, index="predicted_product_line", columns="doc_type",
        values="sha256", aggfunc="count", fill_value=0
    ).sort_index()
    pivot.to_csv(pivot_csv)
    return pivot

if __name__ == "__main__":
    p = build_pivot()
    print(f"[report] wrote pivot -> {PIVOT_CSV}")
