"""
deploy_site.py 鈥?Fully automated static site deployment to surge.sh
1. Syncs root HTML/CSS/XML/TXT to deploy/
2. Copies deploy/ + reviews/compare/images from root to temp path
3. Deploys via surge CLI to honestgadgets.surge.sh
"""
import subprocess
import sys
import os
import shutil
import tempfile

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = os.path.join(ROOT_DIR, "deploy")
ENV_FILE = os.path.join(ROOT_DIR, ".env")
DOMAIN = "honestgadgets.surge.sh"
# Try Windows path first, fall back to system PATH (Linux CI)
_SURGE_WIN = "C:/Users/Administrator/AppData/Roaming/npm/surge.cmd"
SURGE = _SURGE_WIN if os.path.exists(_SURGE_WIN) else "surge"

PUBLIC_GLOBS = ["*.html", "*.xml", "*.txt"]
PUBLIC_DIRS = ["reviews", "compare", "images"]


def load_env():
    """Load config from .env file, with os.environ as override."""
    env = {}
    # Read .env file first (lower priority)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    # Merge os.environ (higher priority -- CI secrets override .env)
    for key in ["SURGE_TOKEN", "SURGE_EMAIL", "SITE_URL"]:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def sync_to_deploy():
    """Copy root HTML/XML/TXT to deploy/ (subdirs used directly from root)"""
    print("[SYNC] Copying root public files to deploy/...")
    os.makedirs(DEPLOY_DIR, exist_ok=True)

    import glob
    for pattern in PUBLIC_GLOBS:
        for filepath in glob.glob(os.path.join(ROOT_DIR, pattern)):
            filename = os.path.basename(filepath)
            shutil.copy2(filepath, os.path.join(DEPLOY_DIR, filename))

    total = sum(1 for _ in os.scandir(DEPLOY_DIR))
    print(f"[SYNC] Done. {total} items in deploy/")


def deploy():
    env_cfg = load_env()
    if not env_cfg.get("SURGE_TOKEN"):
        print("[WARN] SURGE_TOKEN not found -- deploy skipped (CI-safe: exit 0)")
        return None

    sync_to_deploy()

    # Build temp dir for surge: deploy/ staging + content dirs from root
    tmp = os.path.join(tempfile.gettempdir(), "surge_deploy")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    shutil.copytree(DEPLOY_DIR, tmp)
    for dirname in PUBLIC_DIRS:
        src = os.path.join(ROOT_DIR, dirname)
        if os.path.exists(src):
            shutil.copytree(src, os.path.join(tmp, dirname))
    print(f"[DEPLOY] Uploading {tmp} -> {DOMAIN} ...")

    # Build environment for surge CLI (needs SURGE_LOGIN + SURGE_TOKEN)
    surge_env = os.environ.copy()
    if env_cfg.get("SURGE_EMAIL"):
        surge_env["SURGE_LOGIN"] = env_cfg["SURGE_EMAIL"]
    if env_cfg.get("SURGE_TOKEN"):
        surge_env["SURGE_TOKEN"] = env_cfg["SURGE_TOKEN"]

    # Run surge with explicit env (CI-safe)
    result = subprocess.run(
        [SURGE, tmp, DOMAIN],
        capture_output=True,
        timeout=300,
        env=surge_env
    )

    # Combine stdout+stderr for checking
    out = (result.stdout or b"") + (result.stderr or b"")
    text = out.decode("utf-8", errors="replace")

    if result.returncode == 0 and (b"Success" in out or b"Published" in out):
        print(f"\n[DEPLOY] SUCCESS! Site live at https://{DOMAIN}")
        return f"https://{DOMAIN}"
    else:
        print(f"[DEPLOY] FAILED (rc={result.returncode})")
        print(text[-500:])
        print("[WARN] Deploy command failed -- CI-safe: exit 0")
        return None


if __name__ == "__main__":
    deploy()
