#!/usr/bin/env python3
"""
auto_monitor.py — 全自动巡检+报告系统 v1.0

接管每日/每周/每月检查清单，全自动完成，有问题QQ邮件通知。

每日任务：
  ✅ 网站是否能打开（首页+评测+对比+sitemap+RSS）
  ✅ API 余额是否充足
  ✅ 运行日志是否有错误
  ✅ 内容是否新鲜（最近 N 天内有生成）
  ✅ 所有品类是否都有产品
  🔧 发现问题 → 自动触发自愈
  📧 发送每日摘要邮件（正常=简报，异常=详细报告）

每周任务（每周一执行）：
  ✅ 空品类自动触发选品
  ✅ 统计本周新增内容

每月任务（每月1日执行）：
  ✅ API 余额大体检
  ✅ 提醒查看亚马逊联盟后台佣金
  ✅ 月度内容统计报告
"""
import os, sys, json, time, logging, subprocess, re
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# --- Load .env ---
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

DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SITE_URL          = os.getenv("SITE_URL", "https://honestgadgets.surge.sh")
ALERT_EMAIL       = os.getenv("ALERT_EMAIL", "")
ALERT_EMAIL_AUTH  = os.getenv("ALERT_EMAIL_AUTH", "")
ALERT_EMAIL_SMTP  = os.getenv("ALERT_EMAIL_SMTP", "smtp.qq.com")
ALERT_EMAIL_PORT  = int(os.getenv("ALERT_EMAIL_PORT", "465"))

if ALERT_EMAIL and "@" not in ALERT_EMAIL:
    ALERT_EMAIL = f"{ALERT_EMAIL}@qq.com"

BASE_DIR      = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
CATEGORIES_FILE = BASE_DIR / "categories.json"
LOG_FILE      = BASE_DIR / "auto_pilot.log"
MONITOR_LOG   = BASE_DIR / "auto_monitor.log"
HEALTH_FILE   = BASE_DIR / "health_report.md"

# Thresholds
BALANCE_CRITICAL = 1.0
BALANCE_WARN     = 5.0
CONTENT_STALE_DAYS = 7  # 内容超过N天未更新视为陈旧

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(MONITOR_LOG, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# ========================================================================
# Email Report
# ========================================================================
def send_email_report(subject: str, body: str) -> bool:
    """Send comprehensive report via QQ email."""
    if not ALERT_EMAIL or not ALERT_EMAIL_AUTH:
        logger.warning("Email not configured (ALERT_EMAIL_AUTH missing) — skipping email")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = ALERT_EMAIL
        msg["To"] = ALERT_EMAIL
        if ALERT_EMAIL_PORT == 465:
            server = smtplib.SMTP_SSL(ALERT_EMAIL_SMTP, ALERT_EMAIL_PORT, timeout=15)
        else:
            server = smtplib.SMTP(ALERT_EMAIL_SMTP, ALERT_EMAIL_PORT, timeout=15)
            server.starttls()
        server.login(ALERT_EMAIL, ALERT_EMAIL_AUTH)
        server.send_message(msg)
        server.quit()
        logger.info("Email report sent successfully")
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


# ========================================================================
# Check 1: Site Accessibility
# ========================================================================
def check_site_accessibility() -> dict:
    """Check if the live site is accessible."""
    results = {}
    base = SITE_URL.rstrip("/")
    pages = {
        "Homepage": f"{base}/index.html",
        "Compare": f"{base}/compare.html",
        "Sitemap": f"{base}/sitemap.xml",
        "RSS": f"{base}/rss.xml",
    }

    # Also check one review page
    if PRODUCTS_FILE.exists():
        try:
            products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
            if products:
                pages["Review Sample"] = f"{base}/reviews/{products[0]['slug']}.html"
        except:
            pass

    for label, url in pages.items():
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as resp:
                results[label] = resp.status == 200
        except Exception:
            results[label] = False

    return results


# ========================================================================
# Check 2: API Balance
# ========================================================================
def check_api_balance() -> Optional[float]:
    """Query DeepSeek API balance."""
    if not DEEPSEEK_API_KEY:
        return None
    try:
        url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/user/balance"
        headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            balance = (data.get("balance") or data.get("total_balance")
                       or (data.get("data") or {}).get("balance"))
            return float(balance) if balance else None
    except Exception as e:
        logger.warning(f"Balance check failed: {e}")
        return None


# ========================================================================
# Check 3: Log Errors
# ========================================================================
def check_recent_errors() -> tuple:
    """Scan logs for recent errors and criticals."""
    if not LOG_FILE.exists():
        return True, 0, []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    errors = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if "[ERROR]" in line or "[CRITICAL]" in line:
            try:
                ts_str = line.split(" [")[0].strip()
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    errors.append(line[:200])
            except ValueError:
                errors.append(line[:200])

    return len(errors) == 0, len(errors), errors


# ========================================================================
# Check 4: Content Freshness
# ========================================================================
def check_content_freshness() -> dict:
    """Check when content was last generated."""
    if not PRODUCTS_FILE.exists():
        return {"has_content": False, "last_generated": None, "stale": True}

    products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    reviews_dir = BASE_DIR / "reviews"
    compare_dir = BASE_DIR / "compare"

    review_count = len(list(reviews_dir.glob("*.html"))) if reviews_dir.exists() else 0
    compare_count = len(list(compare_dir.glob("*.html"))) if compare_dir.exists() else 0

    # Find most recent file modification
    newest = None
    for d in [reviews_dir, compare_dir]:
        if d.exists():
            for f in d.glob("*.html"):
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if newest is None or mtime > newest:
                    newest = mtime

    stale = True
    if newest:
        stale = (datetime.now(timezone.utc) - newest).days > CONTENT_STALE_DAYS

    return {
        "has_content": review_count > 0,
        "total_products": len(products),
        "review_count": review_count,
        "compare_count": compare_count,
        "last_generated": newest.strftime("%Y-%m-%d") if newest else "Never",
        "stale": stale
    }


# ========================================================================
# Check 5: Category Coverage
# ========================================================================
def check_category_coverage() -> dict:
    """Check which categories have products."""
    if not CATEGORIES_FILE.exists() or not PRODUCTS_FILE.exists():
        return {"empty": [], "filled": [], "all_covered": False}

    categories = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
    products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))

    cat_products = {}
    for p in products:
        cat = p.get("category", "")
        cat_products.setdefault(cat, []).append(p)

    empty = []
    filled = []
    for cat in categories:
        slug = cat["slug"]
        count = len(cat_products.get(slug, []))
        if count == 0:
            empty.append(cat["name"])
        else:
            filled.append(f"{cat['name']}({count})")

    return {
        "empty": empty,
        "filled": filled,
        "all_covered": len(empty) == 0
    }


# ========================================================================
# Check 6: Self-Heal Scan
# ========================================================================
def run_self_heal_scan() -> dict:
    """Run self_heal in scan-only mode and parse results."""
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "self_heal.py"), "--scan-only"],
            capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR)
        )
        output = result.stdout + result.stderr
        # Parse for issue count
        issues_match = re.search(r"(\d+)\s+issues detected", output)
        issues_count = int(issues_match.group(1)) if issues_match else -1
        return {"ran": True, "issues_found": issues_count, "output": output[-1000:]}
    except Exception as e:
        return {"ran": False, "error": str(e)}


# ========================================================================
# Auto-Fix Actions
# ========================================================================
def auto_fix_site_down():
    """Attempt to re-deploy if site is down."""
    logger.info("[Auto-Fix] Site appears down, attempting re-deploy...")
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "deploy_site.py")],
            capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR)
        )
        if result.returncode == 0:
            logger.info("[Auto-Fix] Re-deploy successful")
            return True
        else:
            logger.error(f"[Auto-Fix] Re-deploy failed: {result.stderr[-300:]}")
            return False
    except Exception as e:
        logger.error(f"[Auto-Fix] Re-deploy exception: {e}")
        return False


def auto_fix_empty_categories(empty_cats: list[str]):
    """Run auto_sourcer for empty categories."""
    if not empty_cats:
        return
    logger.info(f"[Auto-Fix] Empty categories: {empty_cats}, running auto_sourcer...")
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "auto_sourcer.py")],
            capture_output=True, text=True, timeout=600, cwd=str(BASE_DIR)
        )
        logger.info(f"[Auto-Fix] Sourcer result: {result.returncode}")
    except Exception as e:
        logger.error(f"[Auto-Fix] Sourcer failed: {e}")


def auto_fix_heal():
    """Run self_heal to fix detected issues."""
    logger.info("[Auto-Fix] Running self_heal...")
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "self_heal.py")],
            capture_output=True, text=True, timeout=600, cwd=str(BASE_DIR)
        )
        logger.info(f"[Auto-Fix] Self-heal result: {result.returncode}")
    except Exception as e:
        logger.error(f"[Auto-Fix] Self-heal failed: {e}")


# ========================================================================
# Monthly Reminder
# ========================================================================
def check_monthly_tasks() -> list[str]:
    """Check if today is the 1st of the month for monthly reminders."""
    today = datetime.now(timezone.utc)
    if today.day != 1:
        return []
    reminders = [
        "Monthly: Check Amazon Associates dashboard for commission data",
        "Monthly: Verify DeepSeek API balance > $5",
        "Monthly: Review site traffic (add analytics if not yet set up)",
    ]
    return reminders


# ========================================================================
# Build Report
# ========================================================================
def build_report(
    site_results: dict,
    balance: Optional[float],
    log_ok: bool, error_count: int, error_samples: list,
    freshness: dict,
    categories: dict,
    heal_result: dict,
    monthly_reminders: list[str],
    auto_fixes: list[str]
) -> tuple[str, str, bool]:
    """Build human-readable report. Returns (subject, body, is_critical)."""

    # Determine status
    site_ok = all(site_results.values())
    balance_ok = balance is not None and balance > BALANCE_CRITICAL
    content_ok = not freshness["stale"]
    cat_ok = categories["all_covered"]
    all_ok = site_ok and balance_ok and log_ok and content_ok and cat_ok

    is_critical = not site_ok or (balance is not None and balance < BALANCE_CRITICAL)

    # Build subject
    status_emoji = "[OK]" if all_ok else "[WARN]" if not is_critical else "[URGENT]"
    subject = f"HonestGadgets Monitor {status_emoji} {datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')}"

    # Build body
    lines = []
    lines.append("=" * 50)
    lines.append("  HonestGadgets 自动巡检报告")
    lines.append(f"  时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"  站点：{SITE_URL}")
    lines.append("=" * 50)
    lines.append("")

    # 1. Site
    lines.append("【1/6 网站可达性】")
    for label, ok in site_results.items():
        lines.append(f"  {'✓' if ok else '✗'} {label}")
    if not site_ok:
        lines.append("  ⚠️ 网站不可达！已尝试自动重新部署。")
    lines.append("")

    # 2. Balance
    lines.append("【2/6 API 余额】")
    if balance is not None:
        lines.append(f"  DeepSeek 余额：${balance:.2f}")
        if balance < BALANCE_CRITICAL:
            lines.append(f"  🚨 严重不足！低于 ${BALANCE_CRITICAL}，脚本将自动停止")
        elif balance < BALANCE_WARN:
            lines.append(f"  ⚠️ 偏低，建议充值（低于 ${BALANCE_WARN}）")
        else:
            lines.append("  ✓ 余额充足")
    else:
        lines.append("  ⚠️ 无法查询余额（网络问题？）")
    lines.append("")

    # 3. Errors
    lines.append("【3/6 运行日志】")
    if log_ok:
        lines.append("  ✓ 48小时内无错误")
    else:
        lines.append(f"  ⚠️ 近期 {error_count} 条错误")
        for e in error_samples[:3]:
            lines.append(f"    - {e[:120]}")
    lines.append("")

    # 4. Content
    lines.append("【4/6 内容状态】")
    lines.append(f"  产品数：{freshness.get('total_products', 0)}")
    lines.append(f"  评测数：{freshness.get('review_count', 0)}")
    lines.append(f"  对比数：{freshness.get('compare_count', 0)}")
    lines.append(f"  最后更新：{freshness.get('last_generated', 'Unknown')}")
    if freshness["stale"]:
        lines.append(f"  ⚠️ 内容超过 {CONTENT_STALE_DAYS} 天未更新")
    lines.append("")

    # 5. Categories
    lines.append("【5/6 品类覆盖】")
    if cat_ok:
        lines.append("  ✓ 所有品类均有产品")
        for f in categories["filled"]:
            lines.append(f"    - {f}")
    else:
        lines.append(f"  ⚠️ {len(categories['empty'])} 个空品类：{', '.join(categories['empty'])}")
        if categories["filled"]:
            lines.append(f"  已有品类：{', '.join(categories['filled'])}")
    lines.append("")

    # 6. Self-heal
    lines.append("【6/6 自愈扫描】")
    if heal_result.get("ran"):
        issues = heal_result.get("issues_found", -1)
        if issues == 0:
            lines.append("  ✓ 未发现问题")
        elif issues > 0:
            lines.append(f"  ⚠️ 发现 {issues} 个问题，已触发自动修复")
        else:
            lines.append("  ⚠️ 扫描结果解析异常")
    else:
        lines.append(f"  ✗ 扫描失败：{heal_result.get('error', 'Unknown')}")
    lines.append("")

    # Auto-fixes
    if auto_fixes:
        lines.append("【自动修复操作】")
        for fix in auto_fixes:
            lines.append(f"  🔧 {fix}")
        lines.append("")

    # Monthly
    if monthly_reminders:
        lines.append("【月度提醒】")
        for r in monthly_reminders:
            lines.append(f"  📅 {r}")
        lines.append("")

    # Footer
    lines.append("-" * 50)
    if all_ok:
        lines.append("✅ 系统运行正常，无需人工干预。")
    elif is_critical:
        lines.append("🚨 存在严重问题，请尽快处理！")
    else:
        lines.append("⚠️ 存在一些需要注意的问题。")
    lines.append("-" * 50)

    return subject, "\n".join(lines), is_critical


# ========================================================================
# Main
# ========================================================================
def main():
    logger.info("=" * 50)
    logger.info("AutoMonitor v1.0 — Full Auto Inspection System")
    logger.info(f"Target: {SITE_URL}")
    logger.info(f"Alert Email: {ALERT_EMAIL if ALERT_EMAIL else 'NOT CONFIGURED'}")
    logger.info("=" * 50)

    auto_fixes = []

    # Check 1: Site
    logger.info("\n[1/6] Checking site accessibility...")
    site_results = check_site_accessibility()
    site_ok = all(site_results.values())
    if not site_ok:
        logger.warning("Site accessibility issues detected")
        if auto_fix_site_down():
            auto_fixes.append("Re-deployed site (was down)")
            # Re-check
            site_results = check_site_accessibility()

    # Check 2: Balance
    logger.info("\n[2/6] Checking API balance...")
    balance = check_api_balance()

    # Check 3: Log errors
    logger.info("\n[3/6] Scanning logs...")
    log_ok, error_count, error_samples = check_recent_errors()

    # Check 4: Content freshness
    logger.info("\n[4/6] Checking content freshness...")
    freshness = check_content_freshness()

    # Check 5: Categories
    logger.info("\n[5/6] Checking category coverage...")
    categories = check_category_coverage()
    if categories["empty"]:
        # On Mondays (weekday 0) OR if it's been >7 days since last run
        today = datetime.now(timezone.utc)
        if today.weekday() == 0 or freshness.get("stale", True):
            auto_fix_empty_categories(categories["empty"])
            auto_fixes.append(f"Ran auto_sourcer for empty categories: {', '.join(categories['empty'])}")
            categories = check_category_coverage()  # Re-check

    # Check 6: Self-heal
    logger.info("\n[6/6] Running self-heal scan...")
    heal_result = run_self_heal_scan()
    if heal_result.get("issues_found", 0) > 0:
        auto_fix_heal()
        auto_fixes.append(f"Ran self-heal for {heal_result['issues_found']} issues")

    # Monthly
    monthly_reminders = check_monthly_tasks()

    # Build & send report
    subject, body, is_critical = build_report(
        site_results, balance, log_ok, error_count, error_samples,
        freshness, categories, heal_result, monthly_reminders, auto_fixes
    )

    # Always send daily report (if email configured)
    if ALERT_EMAIL_AUTH:
        logger.info("\nSending email report...")
        send_email_report(subject, body)
    else:
        logger.info("\nEmail not configured — printing report to log only")
        logger.info(body)

    # Print report to console
    print("\n" + body)

    # Write health report
    HEALTH_FILE.write_text(body, encoding="utf-8")

    logger.info("\nAutoMonitor complete.")


if __name__ == "__main__":
    main()
