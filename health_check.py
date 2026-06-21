#!/usr/bin/env python3
"""
Independent site health monitor v2.1
Checks: homepage / reviews / comparisons / sitemap / RSS / log errors
Discovers issues -> sends phone notification -> auto-triggers self-healing
"""
import os, json, urllib.request, urllib.parse, logging
import subprocess, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k not in os.environ:
                    os.environ[k] = v

SITE_URL        = os.getenv("SITE_URL", "https://honestgadgets.surge.sh")
ALERT_EMAIL     = os.getenv("ALERT_EMAIL", "")
ALERT_EMAIL_AUTH = os.getenv("ALERT_EMAIL_AUTH", "")
PUSH_DEER_KEY   = os.getenv("PUSH_DEER_KEY", "")
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "")

BASE_DIR  = Path(__file__).parent
LOG_FILE  = BASE_DIR / "auto_pilot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def send_email_alert(title: str, content: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText
    if not ALERT_EMAIL or not ALERT_EMAIL_AUTH:
        return False
    try:
        smtp = os.getenv("ALERT_EMAIL_SMTP", "smtp.qq.com")
        port = int(os.getenv("ALERT_EMAIL_PORT", "465"))
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = ALERT_EMAIL
        msg["To"] = ALERT_EMAIL
        if port == 465:
            server = smtplib.SMTP_SSL(smtp, port, timeout=10)
        else:
            server = smtplib.SMTP(smtp, port, timeout=10)
            server.starttls()
        server.login(ALERT_EMAIL, ALERT_EMAIL_AUTH)
        server.send_message(msg)
        server.quit()
        return True
    except Exception:
        return False

def send_alert(title: str, content: str):
    if send_email_alert(title, content):
        return True
    if PUSH_DEER_KEY:
        try:
            url = (f"https://api2.pushdeer.com/message/push"
                   f"?pushkey={PUSH_DEER_KEY}"
                   f"&text={urllib.parse.quote(title)}"
                   f"&desp={urllib.parse.quote(content)}")
            urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10)
            logger.info("PushDeer notification sent")
        except Exception as e:
            logger.warning(f"PushDeer failed: {e}")
    if SERVER_CHAN_KEY:
        try:
            url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
            data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=10)
            logger.info("ServerChan notification sent")
        except Exception as e:
            logger.warning(f"ServerChan failed: {e}")


def check_page(url: str, label: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
            logger.info(f"  {'[OK]' if ok else '[WARN]'} {label}: HTTP {resp.status}")
            return ok
    except Exception as e:
        logger.error(f"  [FAIL] {label}: {e}")
        return False


def check_xml_file(path: Path, label: str, required_tag: str) -> bool:
    if not path.exists():
        logger.error(f"  [FAIL] {label}: file missing")
        return False
    content = path.read_text(encoding="utf-8")
    if required_tag in content:
        count = content.count("<url>") if required_tag == "<url>" else content.count("<item>")
        logger.info(f"  [OK] {label}: OK ({count} entries)")
        return True
    logger.warning(f"  [WARN] {label}: missing {required_tag}")
    return False


def check_log_for_failures() -> tuple:
    if not LOG_FILE.exists():
        logger.info("  [INFO] No log file (may not have run yet)")
        return True, "No log"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    failures = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if "[ERROR]" in line or "[CRITICAL]" in line:
            try:
                ts_str = line.split(" [")[0].strip()
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    failures.append(line[:200])
            except ValueError:
                failures.append(line[:200])
    if failures:
        logger.warning(f"  [WARN] {len(failures)} recent errors")
        return False, f"{len(failures)} errors"
    logger.info("  [OK] Log: clean (48h)")
    return True, "Clean"


def trigger_self_heal():
    """Launch self_heal.py to auto-fix detected issues."""
    logger.info("[Self-Heal] Triggering self_heal.py...")
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "self_heal.py")],
            capture_output=True, text=True, timeout=600, cwd=str(BASE_DIR)
        )
        if result.returncode == 0:
            logger.info("[Self-Heal] Completed successfully")
        else:
            logger.warning(f"[Self-Heal] rc={result.returncode}")
            if result.stderr:
                logger.warning(result.stderr[-500:])
    except Exception as e:
        logger.error(f"[Self-Heal] Launch failed: {e}")


def main():
    logger.info(f"Site Health Check v2.1: {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Target: {SITE_URL}")

    checks = []
    base = SITE_URL.rstrip("/")

    logger.info("\n[1/6] Homepage...")
    checks.append(("Homepage", check_page(f"{base}/index.html", "Homepage")))

    logger.info("\n[2/6] Review page...")
    prod_file = BASE_DIR / "products.json"
    has_review = False
    first_slug = ""
    if prod_file.exists():
        products = json.loads(prod_file.read_text(encoding="utf-8"))
        for p in products:
            slug = p.get("slug", "")
            if (BASE_DIR / "reviews" / f"{slug}.html").exists():
                has_review = True
                first_slug = slug
                break
    reviews_ok = check_page(f"{base}/reviews/{first_slug}.html", "Review page") if has_review else True
    checks.append(("Review", reviews_ok))

    logger.info("\n[3/6] Compare page...")
    compare_ok = check_page(f"{base}/compare.html", "Compare index")
    checks.append(("Compare", compare_ok))

    logger.info("\n[4/6] sitemap.xml...")
    checks.append(("Sitemap", check_xml_file(BASE_DIR / "sitemap.xml", "sitemap.xml", "<url>")))

    logger.info("\n[5/6] rss.xml...")
    checks.append(("RSS", check_xml_file(BASE_DIR / "rss.xml", "rss.xml", "<item>")))

    logger.info("\n[6/6] Runtime log...")
    log_ok, log_detail = check_log_for_failures()
    checks.append(("Log", log_ok))

    failed = [(n, r) for n, r in checks if not r]
    if not failed:
        logger.info("\n[PASS] All 6 checks passed")
        logger.info("[Self-Heal] No issues -- skipping auto-heal")
    else:
        names = [n for n, _ in failed]
        logger.error(f"\n[FAIL] {len(failed)} checks: {', '.join(names)}")
        send_alert(f"Site Alert: {len(failed)} checks failed",
                   f"Site: {SITE_URL}\nFailed: {', '.join(names)}\nLog: {log_detail}")

        # Auto-trigger self-healing
        trigger_self_heal()


if __name__ == "__main__":
    main()
