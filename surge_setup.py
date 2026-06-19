"""
surge_setup.py — Surge.sh One-Time Account Setup + Auto Deploy
Usage: python surge_setup.py email@example.com password
   or: set SURGE_EMAIL + SURGE_PASSWORD env vars
After setup, all deployments are fully automated via deploy_site.py
"""
import subprocess
import sys
import os

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
SURGE = "C:/Users/Administrator/AppData/Roaming/npm/surge.cmd"


def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    return env


def save_env(updates):
    existing = load_env()
    existing.update(updates)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        for key, val in existing.items():
            f.write(f"{key}={val}\n")
    print(f"[OK] Saved to {ENV_FILE}")


def setup():
    print("=" * 60)
    print("  Surge.sh Auto Deploy - One-Time Setup")
    print("  Free static hosting at honestgadgets.surge.sh")
    print("=" * 60)
    print()

    env = load_env()
    if env.get("SURGE_TOKEN"):
        print("[INFO] SURGE_TOKEN already exists. Run deploy_site.py to deploy.")
        return

    # Check surge is installed
    try:
        result = subprocess.run([SURGE, "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print("[ERROR] Surge not found at", SURGE)
            sys.exit(1)
    except FileNotFoundError:
        print("[ERROR] Surge not found at", SURGE)
        sys.exit(1)

    # Get credentials
    email = os.environ.get("SURGE_EMAIL", "").strip()
    password = os.environ.get("SURGE_PASSWORD", "").strip()
    if not email and len(sys.argv) >= 3:
        email = sys.argv[1]
        password = sys.argv[2]
    if not email or "@" not in email:
        print("[ERROR] Usage: python surge_setup.py email@example.com password")
        sys.exit(1)
    if len(password) < 6:
        print("[ERROR] Password must be >= 6 characters")
        sys.exit(1)

    print(f"[STEP 1] Email: {email}")
    print(f"[STEP 1] Password: {'*' * len(password)}")
    print()

    # Login
    print("[STEP 2] Logging in to surge.sh...")
    try:
        result = subprocess.run(
            [SURGE, "login"],
            input=f"{email}\n{password}\n",
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout + result.stderr
        if "Success" in output or "token" in output.lower() or "Logged in" in output:
            print("[OK] Login successful!")
        elif "Invalid" in output or "error" in output.lower():
            print(f"[ERROR] Login failed: {output[:500]}")
            sys.exit(1)
        else:
            print(f"[INFO] Output: {output[:300]}")
    except subprocess.TimeoutExpired:
        print("[ERROR] Login timed out. Check network.")
        sys.exit(1)

    # Get token
    print()
    print("[STEP 3] Getting automation token...")
    try:
        result = subprocess.run(
            [SURGE, "token"],
            capture_output=True,
            text=True,
            timeout=15
        )
        token_raw = result.stdout.strip()
        lines = token_raw.split("\n")
        token = ""
        for line in lines:
            line = line.strip()
            if line and len(line) > 20:
                # Filter ANSI escape codes
                clean = ""
                skip = False
                for ch in line:
                    if ch == "\x1b":
                        skip = True
                    elif skip and ch == "m":
                        skip = False
                    elif not skip:
                        clean += ch
                if len(clean) > 20:
                    token = clean
                    break
        if not token:
            token = token_raw

        if token and len(token) > 20:
            print(f"[OK] Token: {token[:10]}...{token[-10:]}")
            save_env({
                "SURGE_TOKEN": token,
                "SURGE_EMAIL": email,
                "SITE_URL": "https://honestgadgets.surge.sh"
            })
        else:
            print(f"[ERROR] Failed to get token. Raw: {token_raw[:200]}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[ERROR] Token retrieval timed out.")
        sys.exit(1)

    # Deploy
    print()
    print("[STEP 4] Deploying site...")
    deploy_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_site.py")
    result = subprocess.run(
        [sys.executable, deploy_script],
        capture_output=True,
        text=True,
        timeout=180
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)

    print()
    print("=" * 60)
    print("  SETUP COMPLETE!")
    print("  Site URL: https://honestgadgets.surge.sh")
    print()
    print("  Auto-deploy: added to auto_pilot.py Phase 4")
    print("=" * 60)


if __name__ == "__main__":
    setup()
