
# -*- coding: utf-8 -*-
import os, re, json
from pathlib import Path
from typing import Tuple, List, Dict, Any

import pandas as pd
from PyPDF2 import PdfReader

from .config import PRODUCT_LINES, PATTERNS, DOC_TYPES, LANG_FLAGS

# Adjust these to your repo
ARCHIVE_ROOT = Path("archive")         # your crawler output root
OUTPUT_DIR   = Path("output")
SYMLINK_ROOT = Path("categorized")     # optional
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SYMLINK_ROOT.mkdir(parents=True, exist_ok=True)

MANIFEST_CSV = OUTPUT_DIR / "manifest.rules.csv"  # rules-first
FINAL_CSV    = OUTPUT_DIR / "manifest.csv"        # after ML (if used) – written by ml_fallback.py
PIVOT_CSV    = OUTPUT_DIR / "report_by_product_line.csv"

def _extract_text_head(pdf_path: Path, max_pages: int = 3, max_chars: int = 4000) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        text = []
        for page in reader.pages[:max_pages]:
            t = page.extract_text() or ""
            text.append(t)
        return " ".join(text)[:max_chars]
    except Exception:
        return ""

def _guess_doc_type(text: str) -> str:
    for name, rx in DOC_TYPES:
        if rx.search(text):
            return name
    return ""

def _guess_language(text: str) -> str:
    for name, rx in LANG_FLAGS:
        if rx.search(text):
            return name
    return "english"

def _extract_models(text: str) -> List[str]:
    models = set()
    for m in re.findall(r"\bU[A-Z]{0,3}\d{0,4}[A-Z]?\b", text, re.I):
        models.add(m.upper())
    for m in re.findall(r"\bD[HL]\d{2,4}[A-Z0-9.\-]*\b", text, re.I):
        models.add(m.upper())
    for m in re.findall(r"\b(44|54|64|66|76|80|86|90)\s*PRO(?:\s*\w+)*", text, re.I):
        s = m if isinstance(m, str) else " ".join(m)
        if s: models.add(s.upper())
    for m in re.findall(r"\bPP\s*\d+\b", text, re.I):
        models.add(m.upper())
    for m in re.findall(r"\bCG[0-9 ]*\b", text, re.I):
        models.add(m.upper())
    return list(models)[:12]

def _logical_title_from_path(p: Path) -> str:
    name = p.name
    parts = name.split("__")
    if len(parts) >= 3:
        return parts[1].replace("_", " ").strip()
    name = re.sub(r"__sha256_[0-9a-f]{8,64}", "", name, flags=re.I)
    return Path(name).stem.replace("_", " ").strip()

def classify_product_line(text: str) -> Tuple[str, float, List[str]]:
    # Deterministic precedence: Flight > Rack > Door > Under > PP > Other
    reasons = []
    for line in PRODUCT_LINES:
        if line == "Other":
            continue
        for rx in PATTERNS.get(line, []):
            if rx.search(text):
                reasons.append(f"{line}: /{rx.pattern}/")
                return line, 0.9, reasons
    return "Other", 0.5, reasons or ["default: no rule matched"]

def iter_pdfs(root: Path):
    for p in root.rglob("*.pdf"):
        yield p

def run_rules_only(write_symlinks: bool = True) -> pd.DataFrame:
    rows = []
    for pdf in iter_pdfs(ARCHIVE_ROOT):
        vendor = pdf.parts[1] if len(pdf.parts) >= 2 else ""
        seed_hint = pdf.parts[2] if len(pdf.parts) >= 3 else ""
        title = _logical_title_from_path(pdf)

        head_text = _extract_text_head(pdf)
        combined = f"{title}\n{head_text}"

        product_line, conf, reasons = classify_product_line(combined)
        doc_type = _guess_doc_type(combined)
        lang = _guess_language(combined)
        models = _extract_models(combined)

        sha = ""
        m = re.search(r"sha256_([0-9a-f]{8,64})", pdf.name, re.I)
        if m: sha = m.group(1)

        row = {
            "vendor": vendor or "Champion",
            "seed_hint": seed_hint,
            "predicted_product_line": product_line,
            "rule_confidence": conf,
            "reasons": "; ".join(reasons),
            "doc_type": doc_type,
            "language": lang,
            "model_family": "|".join(models),
            "title": title,
            "file_path": str(pdf),
            "sha256": sha,
        }
        rows.append(row)

        if write_symlinks:
            dest = SYMLINK_ROOT / product_line
            dest.mkdir(parents=True, exist_ok=True)
            link = dest / pdf.name
            try:
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(pdf.resolve())
            except Exception:
                # On Windows or restricted FS, skip
                pass

    df = pd.DataFrame(rows)
    df.to_csv(MANIFEST_CSV, index=False)

    # Simple pivot by product line x doc_type
    if not df.empty:
        pivot = pd.pivot_table(
            df, index="predicted_product_line", columns="doc_type",
            values="sha256", aggfunc="count", fill_value=0
        ).sort_index()
        pivot.to_csv(PIVOT_CSV)
    return df

if __name__ == "__main__":
    df = run_rules_only()
    print(f"[rules] wrote: {MANIFEST_CSV}")
    print(f"[rules] pivot: {PIVOT_CSV}")
