
# -*- coding: utf-8 -*-
import json
from pathlib import Path

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import classification_report
from joblib import dump, load

from .rules import OUTPUT_DIR, ARCHIVE_ROOT, run_rules_only

MODEL_DIR  = OUTPUT_DIR / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "clf.joblib"
LBL_PATH   = MODEL_DIR / "labels.joblib"

MANIFEST_RULES = OUTPUT_DIR / "manifest.rules.csv"
MANIFEST_FINAL = OUTPUT_DIR / "manifest.csv"  # final (after ML)
TEXT_CACHE     = OUTPUT_DIR / "text_cache.parquet"  # optional

def _ensure_rules_manifest() -> pd.DataFrame:
    if MANIFEST_RULES.exists():
        return pd.read_csv(MANIFEST_RULES)
    return run_rules_only(write_symlinks=False)

def _load_or_extract_text(df: pd.DataFrame) -> pd.Series:
    # We already embedded title+first-pages into rules pass;
    # For ML, concatenate best available signals
    # If you saved a text cache, load it; otherwise use title + seed_hint + reasons
    if "title" not in df.columns:
        df["title"] = ""
    if "reasons" not in df.columns:
        df["reasons"] = ""
    # You can extend this to read first-page text from a cache if you store it
    return df["title"].fillna("") + " " + df["reasons"].fillna("") + " " + df["seed_hint"].fillna("")

def train_if_needed(df: pd.DataFrame, min_train: int = 40):
    # Select high-confidence labels
    hi = df[df["rule_confidence"] >= 0.9].copy()
    if hi.shape[0] < min_train:
        print(f"[ml] not enough high-confidence rows to train ({hi.shape[0]} < {min_train}); skipping training.")
        return None, None

    X = _load_or_extract_text(hi)
    y = hi["predicted_product_line"].astype(str).values

    le = LabelEncoder()
    y_enc = le.fit_transform(y)

    pipe = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1,2), min_df=2, max_df=0.95)),
        ("clf", LogisticRegression(max_iter=200, n_jobs=None))
    ])
    pipe.fit(X, y_enc)

    dump(pipe, MODEL_PATH)
    dump(le, LBL_PATH)
    print(f"[ml] model saved -> {MODEL_PATH}")
    return pipe, le

def load_model():
    if MODEL_PATH.exists() and LBL_PATH.exists():
        return load(MODEL_PATH), load(LBL_PATH)
    return None, None

def apply_fallback(df: pd.DataFrame, prob_accept: float = 0.7):
    pipe, le = load_model()
    if pipe is None:
        pipe, le = train_if_needed(df)
        if pipe is None:
            # No model; write rules manifest as final
            df.to_csv(MANIFEST_FINAL, index=False)
            print(f"[ml] no model; wrote rules manifest as final -> {MANIFEST_FINAL}")
            return df

    # Low-confidence to predict
    low = df[df["rule_confidence"] < 0.7].copy()
    if low.empty:
        df.to_csv(MANIFEST_FINAL, index=False)
        print(f"[ml] nothing to predict; wrote final -> {MANIFEST_FINAL}")
        return df

    X_low = _load_or_extract_text(low)
    proba = pipe.predict_proba(X_low)
    pred_idx = proba.argmax(axis=1)
    pred_prob = proba.max(axis=1)
    pred_labels = le.inverse_transform(pred_idx)

    low["ml_predicted_product_line"] = pred_labels
    low["ml_probability"] = pred_prob

    # Accept ML when confident
    accept = low["ml_probability"] >= prob_accept
    df.loc[low.index[accept], "predicted_product_line"] = low.loc[accept, "ml_predicted_product_line"].values
    df.loc[low.index[accept], "ml_applied"] = True
    df.loc[low.index[~accept], "ml_applied"] = False
    df.loc[low.index, "ml_probability"] = low["ml_probability"].values

    df.to_csv(MANIFEST_FINAL, index=False)
    print(f"[ml] wrote final manifest with ML -> {MANIFEST_FINAL}")
    return df

if __name__ == "__main__":
    rules_df = _ensure_rules_manifest()
    final = apply_fallback(rules_df)
