#!/usr/bin/env python3
"""
self_heal.py -- AutoPilot Self-Healing Module v1.0

Self-healing capabilities:
  1. Log analysis: detect failed content generation from auto_pilot.log
  2. Auto-retry: re-generate failed reviews/comparisons with improved prompts
  3. Dead link detection: verify all affiliate links in generated pages
  4. Content integrity: check required sections, CTA buttons, affiliate tags
  5. Auto-fix: repair broken pages without full regeneration
  6. Commit & push: auto-commit fixes to git repo

Philosophy: "Detect, Diagnose, Repair" -- fully autonomous, no human needed.

Usage:
  python self_heal.py                 # Full heal cycle
  python self_heal.py --scan-only     # Only scan, report issues
  python self_heal.py --fix-links     # Only fix broken affiliate links
  python self_heal.py --retry-failed  # Only retry failed content generation
"""
import os, sys, json, time, logging, re, subprocess
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

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
DEEPSEEK_MODEL_ID = os.getenv("DEEPSEEK_MODEL_ID", "deepseek-v4-pro")
AFFILIATE_TAG     = os.getenv("AFFILIATE_TAG", "")
SITE_URL          = os.getenv("SITE_URL", "")

BASE_DIR      = Path(__file__).parent
LOG_FILE      = BASE_DIR / "auto_pilot.log"
PRODUCTS_FILE = BASE_DIR / "products.json"
REVIEWS_DIR   = BASE_DIR / "reviews"
COMPARE_DIR   = BASE_DIR / "compare"
HEAL_LOG      = BASE_DIR / "self_heal.log"

SCAN_ONLY   = "--scan-only" in sys.argv
FIX_LINKS   = "--fix-links" in sys.argv
RETRY_FAILED = "--retry-failed" in sys.argv
DO_ALL       = not any([SCAN_ONLY, FIX_LINKS, RETRY_FAILED])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(HEAL_LOG, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# ========================================================================
# Section 1: Log Analysis -- detect failures
# ========================================================================
def analyze_log() -> dict:
    """Parse auto_pilot.log and identify failed content items."""
    if not LOG_FILE.exists():
        logger.info("No auto_pilot.log found -- nothing to analyze.")
        return {"failed_reviews": [], "failed_compares": [], "quality_issues": []}

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    failed_reviews = []
    failed_compares = []
    quality_issues = []

    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        # Check timestamp
        try:
            ts_str = line.split(" [")[0].strip()
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except ValueError:
            continue

        if "[ERROR]" not in line and "[WARNING]" not in line:
            continue

        # Detect failed review
        if "review" in line.lower() and ("failed" in line.lower() or "all retries" in line.lower()):
            failed_reviews.append(line[:300])

        # Detect failed comparison
        if "vs" in line.lower() and ("failed" in line.lower() or "quality retries" in line.lower()):
            failed_compares.append(line[:300])

        # Detect quality issues
        if "quality" in line.lower() and ("fail" in line.lower() or "retry" in line.lower()):
            quality_issues.append(line[:300])

    logger.info(f"Log analysis: {len(failed_reviews)} failed reviews, "
                f"{len(failed_compares)} failed compares, "
                f"{len(quality_issues)} quality issues")
    return {
        "failed_reviews": failed_reviews,
        "failed_compares": failed_compares,
        "quality_issues": quality_issues
    }


# ========================================================================
# Section 2: Dead Link Detection
# ========================================================================
def check_affiliate_links() -> list[dict]:
    """Scan all generated HTML pages for broken/missing affiliate links."""
    issues = []
    html_files = list(REVIEWS_DIR.glob("*.html")) + list(COMPARE_DIR.glob("*.html"))

    for f in html_files:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        filename = f.name
        affiliate_hrefs = re.findall(r'href=["\']([^"\']*amazon[^"\']*)["\']', content, re.IGNORECASE)
        cta_elements = re.findall(r'class=["\'][^"\']*bg-gradient[^"\']*["\']', content)

        # Check: at least 1 affiliate link present
        if not affiliate_hrefs:
            issues.append({
                "file": str(f), "type": "missing_affiliate",
                "detail": "No Amazon affiliate link found"
            })
        else:
            # Check: affiliate tag present in URLs
            if AFFILIATE_TAG:
                for href in affiliate_hrefs:
                    if AFFILIATE_TAG not in href:
                        issues.append({
                            "file": str(f), "type": "missing_tag",
                            "detail": f"Affiliate tag missing: {href[:100]}"
                        })
                        break

        # Check: at least 1 CTA button
        if not cta_elements:
            issues.append({
                "file": str(f), "type": "missing_cta",
                "detail": "No CTA button found"
            })

    logger.info(f"Link scan: {len(issues)} issues in {len(html_files)} pages")
    return issues


# ========================================================================
# Section 3: Content Integrity Check
# ========================================================================
def check_content_integrity() -> list[dict]:
    """Verify required sections and SEO elements exist in each page."""
    issues = []
    required_sections = {
        "review": ["First Impressions", "Performance", "Downsides", "Final Verdict", "Check Price on Amazon"],
        "compare": ["Specs Face-Off", "Deep Dive", "Real-World Performance", "Final Verdict"]
    }

    for f in REVIEWS_DIR.glob("*.html"):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        # Check required sections
        missing = []
        for section in required_sections["review"]:
            if section.lower() not in content.lower():
                missing.append(section)
        if missing:
            issues.append({
                "file": str(f), "type": "missing_sections",
                "detail": f"Missing: {', '.join(missing)}"
            })

        # Check word count
        words = len(content.split())
        if words < 800:
            issues.append({
                "file": str(f), "type": "low_word_count",
                "detail": f"Word count: {words} < 800"
            })

    for f in COMPARE_DIR.glob("*.html"):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        missing = []
        for section in required_sections["compare"]:
            if section.lower() not in content.lower():
                missing.append(section)
        if missing:
            issues.append({
                "file": str(f), "type": "missing_sections",
                "detail": f"Missing: {', '.join(missing)}"
            })

        words = len(content.split())
        if words < 1000:
            issues.append({
                "file": str(f), "type": "low_word_count",
                "detail": f"Word count: {words} < 1000"
            })

    logger.info(f"Content integrity: {len(issues)} issues found")
    return issues


# ========================================================================
# Section 4: Auto-Fix Engine
# ========================================================================
def call_deepseek(prompt: str, max_tokens: int = 4000) -> str | None:
    """Call DeepSeek API with retry."""
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = json.dumps({
        "model": DEEPSEEK_MODEL_ID,
        "messages": [
            {"role": "system", "content": "You are a native English copywriter. Write conversationally, never use AI cliches."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens, "temperature": 0.75, "stream": False
    }).encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"API attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(30)
    return None


def fix_missing_section(page_path: Path, page_type: str, missing_sections: list[str]) -> bool:
    """Use AI to generate missing sections and inject them into the page."""
    try:
        content = page_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Cannot read {page_path}: {e}")
        return False

    # Build repair prompt
    excerpt = content[:2000] + "..." + content[-2000:]  # Head + tail context
    prompt = f"""The following {page_type} HTML page is missing these sections: {', '.join(missing_sections)}.

Existing page structure (abbreviated):
{excerpt}

Write ONLY the missing sections as HTML. Each should be wrapped in <h2>...</h2> + content. Use the same conversational tone. Include affiliate CTA buttons where appropriate. Output ONLY the HTML for the missing sections, nothing else."""
    
    new_content = call_deepseek(prompt, max_tokens=1500)
    if not new_content:
        logger.error(f"AI failed to generate fix for {page_path.name}")
        return False

    # Inject: find the last </article> or </main> and insert before it
    injection_point = content.rfind("</article>")
    if injection_point == -1:
        injection_point = content.rfind("</main>")
    if injection_point == -1:
        injection_point = content.rfind("</body>")
    if injection_point == -1:
        logger.error(f"No injection point found in {page_path.name}")
        return False

    fixed = content[:injection_point] + "\n" + new_content + "\n" + content[injection_point:]
    page_path.write_text(fixed, encoding="utf-8")
    logger.info(f"  [FIXED] {page_path.name}: added {', '.join(missing_sections)}")
    return True


def fix_missing_tag(page_path: Path, bad_url: str) -> bool:
    """Append affiliate tag to Amazon URLs."""
    try:
        content = page_path.read_text(encoding="utf-8")
    except Exception:
        return False

    if not AFFILIATE_TAG:
        return False

    # Fix URLs missing the tag
    fixed_count = 0
    def add_tag(match):
        nonlocal fixed_count
        full = match.group(0)
        url = match.group(1)
        if AFFILIATE_TAG not in url and "amazon" in url.lower():
            fixed_count += 1
            sep = "&" if "?" in url else "?"
            return f'href="{url}{sep}{AFFILIATE_TAG.lstrip("?&")}"'
        return full

    new_content = re.sub(r'href=["\']([^"\']*amazon[^"\']*)["\']', add_tag, content, flags=re.IGNORECASE)
    if fixed_count > 0:
        page_path.write_text(new_content, encoding="utf-8")
        logger.info(f"  [FIXED] {page_path.name}: {fixed_count} affiliate tags added")
        return True
    return False


def retry_failed_content() -> dict:
    """Re-generate content for products that have no review/compare page."""
    if not PRODUCTS_FILE.exists():
        return {"fixed_reviews": 0, "fixed_compares": 0}

    products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    fixed_reviews = 0
    fixed_compares = 0

    # Re-use auto_pilot's prompt builders (import inline to avoid circular deps)
    sys.path.insert(0, str(BASE_DIR))
    try:
        from auto_pilot import build_review_prompt, build_comparison_prompt, generate_review_html, generate_comparison_html, category_pairs, validate_content_quality, validate_compare_quality, QUALITY_RETRIES
    except ImportError as e:
        logger.error(f"Cannot import auto_pilot: {e}")
        return {"fixed_reviews": 0, "fixed_compares": 0}

    # Fix missing reviews
    for p in products:
        slug = p.get("slug", "")
        existing = REVIEWS_DIR / f"{slug}.html"
        if not existing.exists():
            logger.info(f"Retrying review: {p['model']}")
            prompt = build_review_prompt(p)
            content = call_deepseek(prompt, max_tokens=4000)
            if content and validate_content_quality(content, p)[0]:
                try:
                    generate_review_html(slug, p, content)
                    fixed_reviews += 1
                    logger.info(f"  [RETRIED] Review: {p['model']}")
                except Exception as e:
                    logger.error(f"  Failed: {e}")
            time.sleep(1)

    # Fix missing comparisons
    for a, b in category_pairs(products):
        slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
        existing = COMPARE_DIR / f"{slug}.html"
        if not existing.exists():
            logger.info(f"Retrying compare: {a['model']} vs {b['model']}")
            prompt = build_comparison_prompt(a, b)
            content = call_deepseek(prompt, max_tokens=4000)
            if content and validate_compare_quality(content, a, b)[0]:
                try:
                    generate_comparison_html(slug, a, b, content)
                    fixed_compares += 1
                    logger.info(f"  [RETRIED] Compare: {a['model']} vs {b['model']}")
                except Exception as e:
                    logger.error(f"  Failed: {e}")
            time.sleep(1)

    return {"fixed_reviews": fixed_reviews, "fixed_compares": fixed_compares}


# ========================================================================
# Section 5: Git Auto-Commit
# ========================================================================
def git_commit_fixes() -> bool:
    """Commit any fixes to git repo."""
    try:
        # Check if there are changes
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, cwd=str(BASE_DIR), check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.info("No changes to commit.")
            return True

        changed = [f for f in result.stdout.strip().split("\n") if f and not f.startswith("self_heal.log")]
        if not changed:
            logger.info("No content changes to commit.")
            return True

        subprocess.run(["git", "add"] + changed, cwd=str(BASE_DIR), check=False)
        subprocess.run(
            ["git", "commit", "-m", f"Auto-heal: fixed {len(changed)} files [skip ci]"],
            cwd=str(BASE_DIR), check=False
        )
        logger.info(f"Committed {len(changed)} fixed files.")
        return True
    except Exception as e:
        logger.error(f"Git commit failed: {e}")
        return False


# ========================================================================
# Main
# ========================================================================
def main():
    logger.info("=" * 50)
    logger.info("Self-Heal Module v1.0 -- Starting")
    logger.info(f"Mode: {'SCAN-ONLY' if SCAN_ONLY else 'FIX-LINKS' if FIX_LINKS else 'RETRY-FAILED' if RETRY_FAILED else 'FULL'}")
    logger.info("=" * 50)

    total_issues = 0
    total_fixed = 0

    # Step 1: Analyze log
    logger.info("\n[1/4] Analyzing auto_pilot.log...")
    log_issues = analyze_log()
    total_issues += len(log_issues["failed_reviews"]) + len(log_issues["failed_compares"])

    # Step 2: Scan affiliate links
    logger.info("\n[2/4] Scanning affiliate links...")
    link_issues = check_affiliate_links()
    total_issues += len(link_issues)

    # Step 3: Content integrity
    logger.info("\n[3/4] Checking content integrity...")
    content_issues = check_content_integrity()
    total_issues += len(content_issues)

    if SCAN_ONLY:
        logger.info(f"\n[SCAN-ONLY] {total_issues} issues detected (not fixed)")
        if link_issues:
            for iss in link_issues[:10]:
                logger.info(f"  - {Path(iss['file']).name}: {iss['detail']}")
        if content_issues:
            for iss in content_issues[:10]:
                logger.info(f"  - {Path(iss['file']).name}: {iss['detail']}")
        return

    # Step 4: Apply fixes
    logger.info("\n[4/4] Applying fixes...")

    # 4a: Fix missing sections
    if DO_ALL:
        for iss in content_issues:
            if iss["type"] == "missing_sections":
                fp = Path(iss["file"])
                missing = [s.strip() for s in iss["detail"].replace("Missing: ", "").split(",")]
                page_type = "review" if "reviews" in str(fp) else "comparison"
                if fix_missing_section(fp, page_type, missing):
                    total_fixed += 1
                time.sleep(1)

    # 4b: Fix missing affiliate tags
    if DO_ALL or FIX_LINKS:
        for iss in link_issues:
            if iss["type"] == "missing_tag":
                fp = Path(iss["file"])
                if fix_missing_tag(fp, iss.get("detail", "")):
                    total_fixed += 1

    # 4c: Retry failed content
    if DO_ALL or RETRY_FAILED:
        retry_result = retry_failed_content()
        total_fixed += retry_result["fixed_reviews"] + retry_result["fixed_compares"]

    # Git commit
    if total_fixed > 0:
        git_commit_fixes()

    # Summary
    logger.info(f"\n{'=' * 50}")
    logger.info(f"Self-Heal Complete: {total_issues} issues detected, {total_fixed} fixed")
    if total_issues == 0:
        logger.info("Status: HEALTHY -- no issues found")
    elif total_fixed >= total_issues:
        logger.info("Status: HEALED -- all issues resolved")
    else:
        logger.info(f"Status: PARTIAL -- {total_issues - total_fixed} issues remain (may need manual review)")
    logger.info(f"{'=' * 50}")


if __name__ == "__main__":
    main()
