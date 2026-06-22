#!/usr/bin/env python3
"""
pre_flight.py -- Early configuration check before CI pipeline runs.
Catch missing secrets, broken imports, or config issues BEFORE the 90-minute pipeline.

Usage: python pre_flight.py
Exits 0 on success with warnings, exits 1 on critical failure.
"""
import ast
import sys
import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
PRODUCTS_FILE = BASE_DIR / "products.json"
REQUIRED_FILES = [
    "auto_pilot.py", "self_heal.py", "health_check.py",
    "deploy_site.py", "auto_monitor.py", "auto_sourcer.py",
    "categories.json", "products.json"
]

warnings = []
errors = []

print("=" * 50)
print("  Pre-Flight Check v1.0")
print("=" * 50)

# 1. Check required files exist
print("\n[1/5] File existence...")
for fname in REQUIRED_FILES:
    fpath = BASE_DIR / fname
    if fpath.exists():
        print(f"  [OK] {fname}")
    else:
        errors.append(f"Missing file: {fname}")
        print(f"  [FAIL] {fname}: NOT FOUND")

# 2. Check Python syntax (all .py files)
print("\n[2/5] Python syntax...")
for fpath in BASE_DIR.glob("*.py"):
    try:
        with open(fpath, 'r', encoding='utf-8') as fh:
            ast.parse(fh.read())
        print(f"  [OK] {fpath.name}")
    except SyntaxError as e:
        errors.append(f"{fpath.name}: syntax error: {e}")
        print(f"  [FAIL] {fpath.name}: {e}")
    except Exception as e:
        warnings.append(f"{fpath.name}: {e}")
        print(f"  [WARN] {fpath.name}: {e}")

# 3. Check critical env vars
print("\n[3/5] Environment variables...")
env_vars = {
    "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
    "DEEPSEEK_BASE_URL": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    "DEEPSEEK_MODEL_ID": os.getenv("DEEPSEEK_MODEL_ID", "deepseek-v4-pro"),
    "AFFILIATE_TAG": os.getenv("AFFILIATE_TAG", ""),
    "SITE_URL": os.getenv("SITE_URL", ""),
}

for k, v in env_vars.items():
    if v:
        masked = v[:4] + "***" if k.endswith("_KEY") or k.endswith("_TAG") else v
        print(f"  [OK] {k} = {masked}")
    else:
        if k == "DEEPSEEK_API_KEY":
            errors.append(f"{k} is NOT SET -- content generation will be skipped")
            print(f"  [WARN] {k}: NOT SET (content gen will skip)")
        elif k == "AFFILIATE_TAG":
            warnings.append(f"{k} is NOT SET -- affiliate links won't work")
            print(f"  [WARN] {k}: NOT SET")
        else:
            warnings.append(f"{k} is NOT SET")
            print(f"  [WARN] {k}: NOT SET")

# 4. Check products.json validity
print("\n[4/5] Data files...")
if PRODUCTS_FILE.exists():
    try:
        products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        print(f"  [OK] products.json: {len(products)} products")
    except Exception as e:
        errors.append(f"products.json: invalid JSON: {e}")
        print(f"  [FAIL] products.json: {e}")
else:
    errors.append("products.json not found")
    print(f"  [FAIL] products.json: NOT FOUND")

# 5. Check deploy readiness
print("\n[5/5] Deploy readiness...")
surge_token = os.getenv("SURGE_TOKEN", "")
surge_email = os.getenv("SURGE_EMAIL", "")

# Check if surge is available
import shutil
if shutil.which("surge") or shutil.which("surge.cmd"):
    print(f"  [OK] surge CLI found")
    if surge_token:
        print(f"  [OK] SURGE_TOKEN set")
    else:
        warnings.append("SURGE_TOKEN not set -- deploy may fail")
        print(f"  [WARN] SURGE_TOKEN: NOT SET")
    if surge_email:
        print(f"  [OK] SURGE_EMAIL set")
    else:
        warnings.append("SURGE_EMAIL not set -- deploy may fail")
        print(f"  [WARN] SURGE_EMAIL: NOT SET")
else:
    warnings.append("surge CLI not found -- deploy will be skipped")
    print(f"  [INFO] surge CLI: NOT FOUND (deploy will skip)")

# Summary
print("\n" + "=" * 50)
if errors:
    print(f"[FAIL] {len(errors)} critical error(s):")
    for e in errors:
        print(f"  - {e}")
    print(f"\n[WARN] {len(warnings)} warning(s)")
    print("=" * 50)
    sys.exit(1)

if warnings:
    print(f"[WARN] {len(warnings)} warning(s) (non-fatal):")
    for w in warnings:
        print(f"  - {w}")
    print("\n[PASS] Pre-flight check passed with warnings -- pipeline will continue")
    print("=" * 50)
else:
    print("[PASS] All pre-flight checks passed!")
    print("=" * 50)

sys.exit(0)
