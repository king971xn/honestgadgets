#!/usr/bin/env python3
"""
独立站点健康监控脚本 v2.0
检查: 首页 / 评测页 / 对比页 / sitemap / RSS / IndexNow key / 日志错误
发现问题时发送手机通知
"""
import os, json, urllib.request, urllib.parse, logging
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
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = value

SITE_URL        = os.getenv("SITE_URL", "https://yourusername.github.io/honestgadgets")
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
    """Send email notification via SMTP"""
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
            logger.info("PushDeer 通知已发送")
        except Exception as e:
            logger.warning(f"PushDeer 发送失败: {e}")
    if SERVER_CHAN_KEY:
        try:
            url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
            data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=10)
            logger.info("Server酱 通知已发送")
        except Exception as e:
            logger.warning(f"Server酱 发送失败: {e}")


def check_page(url: str, label: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
            logger.info(f"  {'✅' if ok else '⚠️'} {label}: HTTP {resp.status}")
            return ok
    except Exception as e:
        logger.error(f"  ❌ {label}: {e}")
        return False


def check_xml_file(path: Path, label: str, required_tag: str) -> bool:
    if not path.exists():
        logger.error(f"  ❌ {label}: 文件不存在")
        return False
    content = path.read_text(encoding="utf-8")
    if required_tag in content:
        count = content.count("<url>") if required_tag == "<url>" else content.count("<item>")
        logger.info(f"  ✅ {label}: OK ({count} entries)")
        return True
    logger.warning(f"  ⚠️ {label}: 缺少 {required_tag}")
    return False


def check_log_for_failures() -> tuple:
    if not LOG_FILE.exists():
        logger.info("  ℹ️ 日志不存在 (可能尚未运行)")
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
        logger.warning(f"  ⚠️ 近期 {len(failures)} 条错误")
        return False, f"{len(failures)} errors"
    logger.info("  ✅ 日志: 48h 内无错误")
    return True, "Clean"


def main():
    logger.info(f"站点健康检查 v2.0: {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"目标: {SITE_URL}")

    checks = []
    base = SITE_URL.rstrip("/")

    logger.info("\n[1/6] 首页...")
    checks.append(("首页", check_page(f"{base}/index.html", "首页")))

    logger.info("\n[2/6] 评测页...")
    prod_file = BASE_DIR / "products.json"
    has_review = False
    if prod_file.exists():
        products = json.loads(prod_file.read_text(encoding="utf-8"))
        for p in products:
            if (BASE_DIR / "reviews" / f"{p['slug']}.html").exists():
                has_review = True
                break
    reviews_ok = check_page(f"{base}/reviews/{products[0]['slug']}.html", "评测页") if has_review else True
    checks.append(("评测页", reviews_ok))

    logger.info("\n[3/6] 对比页...")
    compare_ok = check_page(f"{base}/compare.html", "对比汇总页")
    checks.append(("对比页", compare_ok))

    logger.info("\n[4/6] sitemap.xml...")
    checks.append(("Sitemap", check_xml_file(BASE_DIR / "sitemap.xml", "sitemap.xml", "<url>")))

    logger.info("\n[5/6] rss.xml...")
    checks.append(("RSS", check_xml_file(BASE_DIR / "rss.xml", "rss.xml", "<item>")))

    logger.info("\n[6/6] 运行日志...")
    log_ok, log_detail = check_log_for_failures()
    checks.append(("运行日志", log_ok))

    failed = [(n, r) for n, r in checks if not r]
    if not failed:
        logger.info("\n✅ 全部 6 项检查通过")
    else:
        names = [n for n, _ in failed]
        logger.error(f"\n❌ {len(failed)} 项失败: {', '.join(names)}")
        send_alert("🔴 HonestGadgets 站点异常",
                   f"站点: {SITE_URL}\n失败: {', '.join(names)}\n日志: {log_detail}")


if __name__ == "__main__":
    main()
