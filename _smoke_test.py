"""Smoke test helper: verify Python files compile and have required imports.
Called from GitHub Actions workflows to catch NameError before runtime.
Usage: python _smoke_test.py
Exits 0 on success, 1 on failure.
"""
import ast
import sys
import os

REQUIRED_IMPORTS = {
    'auto_pilot.py': ['subprocess'],
    'self_heal.py': ['subprocess'],
    'health_check.py': ['subprocess'],
    'secure_env.py': [],
    'deploy_site.py': ['subprocess'],
    'auto_monitor.py': ['subprocess'],
    'auto_sourcer.py': [],
    'pre_flight.py': [],
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
failed = False

for fname, required in REQUIRED_IMPORTS.items():
    fpath = os.path.join(BASE_DIR, fname)
    if not os.path.exists(fpath):
        continue  # Skip files not in this workflow's scope

    # Check compilation
    try:
        with open(fpath, 'r', encoding='utf-8') as fh:
            ast.parse(fh.read())
    except SyntaxError as e:
        print(f'[FAIL] {fname}: syntax error: {e}')
        failed = True
        continue

    # Check required imports
    try:
        with open(fpath, 'r', encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split('.')[0])
        missing = [r for r in required if r not in imports]
        if missing:
            print(f'[FAIL] {fname}: missing imports: {missing}')
            failed = True
        else:
            print(f'[OK] {fname}')
    except Exception as e:
        print(f'[FAIL] {fname}: {e}')
        failed = True

if failed:
    print('[FAIL] Smoke test failed')
    sys.exit(1)

print('[PASS] All smoke tests passed')
