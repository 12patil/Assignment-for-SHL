"""
build_index.py
────────────────────────────────────────────────────────────────────────────
One-time script to build and persist the FAISS index.
Run before first server start (or in Dockerfile RUN step).

Usage:
    python build_index.py
"""

import sys
from pathlib import Path

# make sure imports resolve from project root
sys.path.insert(0, str(Path(__file__).parent))

from app.retriever import build_and_save_index

if __name__ == "__main__":
    build_and_save_index()
    print("Index built and saved successfully.")
