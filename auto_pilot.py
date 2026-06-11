#!/usr/bin/env python3
"""
================================================================================
  AI 联盟营销评测网站 — 全自动内容生成 & 自愈闭环脚本 v3.0
  模型: DeepSeek V4-Pro  |  前端: HTML5 + Tailwind CSS
================================================================================

完整闭环:
  1. 预检 API 余额 → 2. 读取 products.json
  → 3a. 生成产品评测 (reviews/*.html)
  → 3b. 生成对比页矩阵 (compare/*.html, 5 商品 = 10 篇)
  → 4. 内容质量守卫 → 5. 更新首页 / 对比汇总页
  → 6. 生成 sitemap.xml + rss.xml → 7. 提交 Bing IndexNow
  → 8. 生成健康报告

自愈机制:
  - API 超时/断连 → 自动重试 3 次 (间隔 60s)
  - 3 次全败 → 跳过 + 错误日志 + 继续下一个
  - 内容质量不合格 → 重试生成 (最多 2 次)
  - API 余额不足 → 预检拦截 + 手机通知
  - Bing 提交失败 → 静默重试 1 次，不影响主流程

用法:
  python auto_pilot.py               # 全量: 评测 + 对比 + RSS + IndexNow
  python auto_pilot.py --reviews-only # 仅评测
  python auto_pilot.py --compare-only # 仅对比
  python auto_pilot.py --dry-run      # 预演 (不调 API, 仅检查配置)
================================================================================
"""
import os, sys, json, time, logging, uuid, hashlib
import urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, timezone
from itertools import combinations

def category_pairs(products: list[dict]):
    """Generate comparison pairs only within same category (saves ~90% token cost)"""
    by_cat = {}
    for p in products:
        cat = p.get('category', 'uncategorized')
        by_cat.setdefault(cat, []).append(p)
    MAX_COMPARE = 8  # 每品类最多8个产品参与对比，防SEO稀释
    for cat, cat_products in by_cat.items():
        if len(cat_products) < 2:
            continue
        # 按添加时间排序，取最新的MAX_COMPARE个
        sorted_products = sorted(cat_products, key=lambda p: p.get('added', ''), reverse=True)
        top = sorted_products[:MAX_COMPARE]
        yield from combinations(top, 2)
from typing import Optional

# ========================================================================
# 加载 .env
# ========================================================================
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
                    os.environ[key] = v

# ========================================================================
# 配置项
# ========================================================================
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL_ID = os.getenv("DEEPSEEK_MODEL_ID", "deepseek-v4-pro")
AFFILIATE_TAG     = os.getenv("AFFILIATE_TAG", "")
PUSH_DEER_KEY     = os.getenv("PUSH_DEER_KEY", "")
SERVER_CHAN_KEY   = os.getenv("SERVER_CHAN_KEY", "")
ALERT_EMAIL       = os.getenv("ALERT_EMAIL", "")
ALERT_EMAIL_AUTH  = os.getenv("ALERT_EMAIL_AUTH", "")
ALERT_EMAIL_SMTP  = os.getenv("ALERT_EMAIL_SMTP", "smtp.qq.com")
ALERT_EMAIL_PORT  = int(os.getenv("ALERT_EMAIL_PORT", "465"))
if ALERT_EMAIL and '@' not in ALERT_EMAIL:
    ALERT_EMAIL = f'{ALERT_EMAIL}@qq.com'

SITE_URL          = os.getenv("SITE_URL", "https://yourusername.github.io/honestgadgets")

# 路径
BASE_DIR      = Path(__file__).parent
PRODUCTS      = BASE_DIR / "products.json"
REVIEWS_DIR   = BASE_DIR / "reviews"
COMPARE_DIR   = BASE_DIR / "compare"
INDEX_FILE    = BASE_DIR / "index.html"
COMPARE_INDEX = BASE_DIR / "compare.html"
LOG_FILE      = BASE_DIR / "auto_pilot.log"
SITEMAP_FILE  = BASE_DIR / "sitemap.xml"
RSS_FILE      = BASE_DIR / "rss.xml"
HEALTH_FILE   = BASE_DIR / "health_report.md"
INDEXNOW_KEY_FILE = BASE_DIR / "indexnow-key.txt"

# 重试参数
MAX_RETRIES         = 3
RETRY_INTERVAL      = 60

# 内容质量
MIN_WORD_COUNT      = 800
MIN_COMPARE_WORDS   = 1000
QUALITY_RETRIES     = 2

# API 余额阈值 (USD)
BALANCE_CRITICAL_THRESHOLD = 1.0
BALANCE_WARN_THRESHOLD     = 5.0

# 确保目录存在
REVIEWS_DIR.mkdir(exist_ok=True)
COMPARE_DIR.mkdir(exist_ok=True)

# ========================================================================
# CLI 参数解析
# ========================================================================
MODE_REVIEWS_ONLY = "--reviews-only" in sys.argv
MODE_COMPARE_ONLY = "--compare-only" in sys.argv
MODE_DRY_RUN      = "--dry-run" in sys.argv

# ========================================================================
# 日志
# ========================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ========================================================================
# 手机通知
# ========================================================================

# ========================================================================
# 邮箱报警通知 (QQ邮箱/163邮箱 均可)
# ========================================================================
def send_email_alert(title: str, content: str) -> bool:
    """通过 SMTP 发送邮件通知"""
    if not ALERT_EMAIL or not ALERT_EMAIL_AUTH:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = ALERT_EMAIL
        msg["To"] = ALERT_EMAIL
        if ALERT_EMAIL_PORT == 465:
            server = smtplib.SMTP_SSL(ALERT_EMAIL_SMTP, ALERT_EMAIL_PORT, timeout=10)
        else:
            server = smtplib.SMTP(ALERT_EMAIL_SMTP, ALERT_EMAIL_PORT, timeout=10)
            server.starttls()
        server.login(ALERT_EMAIL, ALERT_EMAIL_AUTH)
        server.send_message(msg)
        server.quit()
        logger.info("邮件通知发送成功")
        return True
    except Exception as e:
        logger.warning(f"邮件通知发送失败: {e}")
        return False

def send_phone_alert(title: str, content: str) -> bool:
    if send_email_alert(title, content):
        return True
    if PUSH_DEER_KEY:
        try:
            url = (f"https://api2.pushdeer.com/message/push"
                   f"?pushkey={PUSH_DEER_KEY}"
                   f"&text={urllib.parse.quote(title)}"
                   f"&desp={urllib.parse.quote(content)}")
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as resp:
                if resp.status == 200:
                    logger.info("PushDeer 通知发送成功")
                    return True
        except Exception as e:
            logger.warning(f"PushDeer 通知发送失败: {e}")
    if SERVER_CHAN_KEY:
        try:
            url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
            data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("Server酱 通知发送成功")
                    return True
        except Exception as e:
            logger.warning(f"Server酱 通知发送失败: {e}")
    return False


# ========================================================================
# IndexNow Key 管理
# ========================================================================
def get_or_create_indexnow_key() -> str:
    """获取或创建 Bing IndexNow API key"""
    if INDEXNOW_KEY_FILE.exists():
        return INDEXNOW_KEY_FILE.read_text(encoding="utf-8").strip()
    key = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()[:32]
    INDEXNOW_KEY_FILE.write_text(key, encoding="utf-8")
    logger.info(f"已创建 IndexNow key: {INDEXNOW_KEY_FILE}")
    return key


# ========================================================================
# API 余额预检
# ========================================================================
def check_api_balance() -> Optional[float]:
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/user/balance"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            balance = (data.get("balance") or data.get("total_balance")
                       or data.get("data", {}).get("balance"))
            if balance is not None:
                balance = float(balance)
                logger.info(f"API 余额: ${balance:.2f}")
                return balance
            logger.warning(f"无法解析余额: {data}")
            return None
    except Exception as e:
        logger.warning(f"余额查询失败: {e}")
        return None


# ========================================================================
# 内容质量守卫
# ========================================================================
def validate_content_quality(content: str, product: dict, min_words: int = MIN_WORD_COUNT) -> tuple:
    issues = []
    word_count = len(content.split())
    if word_count < min_words:
        issues.append(f"字数不足: {word_count} < {min_words}")
    required_sections = ["First Impressions", "Performance", "Downsides", "Final Verdict"]
    content_lower = content.lower()
    for section in required_sections:
        if section.lower() not in content_lower:
            issues.append(f"缺少必要章节: '{section}'")
    cta_count = (content_lower.count("check price on amazon")
                 + content_lower.count('class="inline-block bg-gradient'))
    if cta_count < 2:
        issues.append(f"CTA 按钮不足: {cta_count} 个")
    banned = ["in today's digital age", "it is important to note", "moreover",
              "furthermore", "in the world of", "when it comes to", "in conclusion",
              "as an ai language model", "as a large language model"]
    for phrase in banned:
        if phrase in content_lower:
            issues.append(f"AI 套话: '{phrase}'")
    model_lower = product["model"].lower()
    # Smart model name check: full name>=1 OR brand>=2 OR short name>=1
    model_full_count = content_lower.count(model_lower)
    brand_name = model_lower.split()[0]
    brand_count = content_lower.count(brand_name)
    model_words = model_lower.split()
    short_model = " ".join(model_words[:min(3, len(model_words))])
    short_count = content_lower.count(short_model) if short_model != model_lower else 0
    effective_mentions = max(model_full_count, short_count, brand_count // 2)
    if effective_mentions < 1:
        issues.append(f"Product name mentions insufficient: full={model_full_count}, short={short_count}, brand={brand_count}")
    if issues:
        return False, "; ".join(issues)
    return True, "OK"


def validate_compare_quality(content: str, a: dict, b: dict) -> tuple:
    """对比页专用质量检查"""
    issues = []
    word_count = len(content.split())
    if word_count < MIN_COMPARE_WORDS:
        issues.append(f"字数不足: {word_count} < {MIN_COMPARE_WORDS}")
    required = ["Specs Face-Off", "Deep Dive", "Real-World Performance", "Final Verdict"]
    content_lower = content.lower()
    for s in required:
        if s.lower() not in content_lower:
            issues.append(f"Missing section: '{s}'")
    # 两款产品的 CTA 都要有
    for model in [a["model"], b["model"]]:
        model_l = model.lower()
        full_cnt = content_lower.count(model_l)
        brand = model_l.split()[0]
        brand_cnt = content_lower.count(brand)
        words = model_l.split()
        short = " ".join(words[:min(3, len(words))])
        short_cnt = content_lower.count(short) if short != model_l else 0
        eff = max(full_cnt, short_cnt, brand_cnt // 2)
        if eff < 1:
            issues.append(f"Product name missing: '{model}'")
    cta_count = (content_lower.count("check price on amazon") + content_lower.count("see on amazon") + content_lower.count("buy on amazon") + content_lower.count('class="inline-block bg-gradient'))
    if cta_count < 1:
        issues.append("CTA buttons missing")
    banned = ["in today's digital age", "it is important to note", "as an ai language model"]
    for phrase in banned:
        if phrase in content_lower:
            issues.append(f"AI 套话: '{phrase}'")
    if issues:
        return False, "; ".join(issues)
    return True, "OK"


# ========================================================================
# DeepSeek API 调用
# ========================================================================
def call_deepseek_api(prompt: str, max_tokens: int = 4000) -> Optional[str]:
    if MODE_DRY_RUN:
        logger.info("[DRY-RUN] 跳过 API 调用")
        return None
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = json.dumps({
        "model": DEEPSEEK_MODEL_ID,
        "messages": [
            {"role": "system", "content": "You are a native English copywriter who writes in-depth, honest product reviews. Your writing is conversational, personal, and NEVER sounds like AI. Use contractions and real-world anecdotes."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens, "temperature": 0.75, "top_p": 0.9, "stream": False
    }).encode("utf-8")
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"API 调用中... (第 {attempt}/{MAX_RETRIES} 次)")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"]
                logger.info(f"API 调用成功，返回 {len(content)} 字符")
                return content
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.reason}"
            logger.error(f"API 调用失败 (HTTP {e.code})")
            if e.code in (401, 403):
                logger.critical("API Key 无效，停止重试")
                return None
        except urllib.error.URLError as e:
            last_error = f"网络错误: {e.reason}"
            logger.error(f"API 调用失败 (网络)")
        except TimeoutError:
            last_error = "请求超时"
            logger.error("API 调用超时")
        except Exception as e:
            last_error = str(e)
            logger.error(f"API 调用失败: {e}")
        if attempt < MAX_RETRIES:
            logger.info(f"等待 {RETRY_INTERVAL}s 后重试...")
            time.sleep(RETRY_INTERVAL)
    logger.error(f"已重试 {MAX_RETRIES} 次，全部失败: {last_error}")
    return None


# ========================================================================
# 评测提示词
# ========================================================================
def build_review_prompt(product: dict) -> str:
    name    = product["name"]
    model   = product["model"]
    price   = product.get("price", "")
    specs   = product.get("specs", {})
    reviews = product.get("reviews", {})
    pos_rev = reviews.get("positive", [])
    neg_rev = reviews.get("negative", [])
    tags    = product.get("tags", [])
    aff_url = product.get("amazon_url", "") + AFFILIATE_TAG

    specs_text = "\n".join([f"  - {k}: {v}" for k, v in specs.items()])
    pos_text   = "\n".join([f"  [{i+1}] {r}" for i, r in enumerate(pos_rev)])
    neg_text   = "\n".join([f"  [{i+1}] {r}" for i, r in enumerate(neg_rev)])

    return f"""Write a detailed, honest, and conversational product review for an affiliate marketing website.

=== PRODUCT INFO ===
Category: {name}
Model: {model}
Price: {price}
Amazon URL: {aff_url}

=== SPECIFICATIONS ===
{specs_text}

=== REAL USER POSITIVE REVIEWS (from Amazon) ===
{pos_text}

=== REAL USER NEGATIVE REVIEWS (from Amazon) ===
{neg_text}

=== SEO TARGET KEYWORDS ===
{", ".join(tags)}

=== WRITING INSTRUCTIONS ===
CRITICAL: Write in a conversational, personal tone that sounds human -- not AI. Follow these rules:

1. STRUCTURE (output ONLY the <article> content, no <html>/<head>/<body> tags):
   - <h2> opening hook: Personal story or relatable pain point
   - <h2> First Impressions & Setup: Unboxing, build quality, setup experience
   - <h2> Performance & Real-World Use: Deep-dive into specs using details from positive reviews
   - <h2> The Downsides: Honest section quoting SPECIFIC complaints from negative reviews
   - <h2> Comparison Table: Clean <table> comparing against 2-3 competitors
   - <h2> Pros & Cons: <ul class="pro-list"> and <ul class="con-list"> lists
   - <h2> Final Verdict: Who this is for AND who should avoid it. End with EXACT CTA button:
     <a href="{aff_url}" class="inline-block bg-gradient-to-r from-orange-500 to-red-500 text-white font-bold text-lg px-8 py-4 rounded-full shadow-lg hover:shadow-xl hover:scale-105 transform transition-all duration-300">Check Price on Amazon →</a>

2. TONE: Use contractions, casual phrases ("Honestly," "Here's the thing," "Not gonna lie"), short paragraphs (2-3 sentences). NEVER use "In today's digital age," "Moreover," "Furthermore," "In conclusion."

3. AFFILIATE: Mention product name 3-4 times. Include 2 additional CTA buttons mid-article. Comparison table should subtly highlight why THIS product is best value.

Output ONLY the <article> HTML content. Start directly with <h2>."""


# ========================================================================
# 对比页提示词
# ========================================================================
def build_comparison_prompt(a: dict, b: dict) -> str:
    """构建 X vs Y 对比评测提示词"""
    aff_a = a.get("amazon_url", "") + AFFILIATE_TAG
    aff_b = b.get("amazon_url", "") + AFFILIATE_TAG

    specs_a = "\n".join([f"  - {k}: {v}" for k, v in a.get("specs", {}).items()])
    specs_b = "\n".join([f"  - {k}: {v}" for k, v in b.get("specs", {}).items()])

    pos_a = "\n".join([f"  [{i+1}] {r}" for i, r in enumerate(a.get("reviews", {}).get("positive", []))])
    neg_a = "\n".join([f"  [{i+1}] {r}" for i, r in enumerate(a.get("reviews", {}).get("negative", []))])
    pos_b = "\n".join([f"  [{i+1}] {r}" for i, r in enumerate(b.get("reviews", {}).get("positive", []))])
    neg_b = "\n".join([f"  [{i+1}] {r}" for i, r in enumerate(b.get("reviews", {}).get("negative", []))])

    return f"""Write a detailed, honest comparison of {a['model']} vs {b['model']} for an affiliate marketing website. Target keywords: "{a['model']} vs {b['model']}", "{a['name']} comparison", "best {a['name']}".

=== PRODUCT A: {a['model']} ===
Price: {a['price']} | Amazon: {aff_a}
Specs:
{specs_a}
Positive reviews:
{pos_a}
Negative reviews:
{neg_a}

=== PRODUCT B: {b['model']} ===
Price: {b['price']} | Amazon: {aff_b}
Specs:
{specs_b}
Positive reviews:
{pos_b}
Negative reviews:
{neg_b}

=== WRITING INSTRUCTIONS ===
CRITICAL: Conversational, personal tone. Output ONLY <article> content.

1. STRUCTURE:
   - <h2> opening hook: Relatable scenario -- reader is torn between these two {a['name'].lower()}s
   - <h2> Specs Face-Off: Clean <table> with <thead>/<tbody> comparing ALL specs side by side. Highlight winner for each row with inline style="color:#16a34a"
   - <h2> {a['model']} Deep Dive: What it does well (from positive reviews), where it falls short (from negative reviews)
   - <h2> {b['model']} Deep Dive: Same honest treatment
   - <h2> Real-World Performance: Which wins for specific scenarios (office, gaming, travel, etc.)
   - <h2> The Hidden Costs: What negative reviews reveal about long-term durability, support, accessories for both
   - <h2> Final Verdict: Clear recommendation for 3 different buyer types. End with BOTH CTA buttons:
     <a href="{aff_a}" class="inline-block bg-gradient-to-r from-orange-500 to-red-500 text-white font-bold text-lg px-8 py-4 rounded-full shadow-lg hover:shadow-xl hover:scale-105 transform transition-all duration-300">Check {a['model']} on Amazon →</a>
     <a href="{aff_b}" class="inline-block bg-gradient-to-r from-orange-500 to-red-500 text-white font-bold text-lg px-8 py-4 rounded-full shadow-lg hover:shadow-xl hover:scale-105 transform transition-all duration-300">Check {b['model']} on Amazon →</a>

2. TONE: Contractions, casual phrases, short paragraphs. NEVER use "Moreover," "Furthermore," "In conclusion."

3. AFFILIATE: Mention each product name 2-3 times. Add 1 extra CTA pair mid-article. Do NOT bias toward one product -- give honest pros/cons for both.

Output ONLY the <article> HTML. Start with <h2>."""


# ========================================================================
# HTML 生成
# ========================================================================
def _html_head(title: str, desc: str, keywords: str, extra_head: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <meta name="description" content="{desc}">
    <meta name="keywords" content="{keywords}">
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        body {{ font-family: 'Inter', sans-serif; background: #f8fafc; color: #1e293b; }}
        article h2 {{ font-size: 1.75rem; font-weight: 700; color: #0f172a; margin: 2rem 0 1rem; line-height: 1.3; }}
        article h3 {{ font-size: 1.25rem; font-weight: 600; color: #334155; margin: 1.5rem 0 0.75rem; }}
        article p  {{ font-size: 1.1rem; line-height: 1.8; color: #334155; margin: 1rem 0; }}
        article ul {{ list-style-type: disc; padding-left: 1.5rem; margin: 0.75rem 0; }}
        article ul li {{ margin: 0.35rem 0; font-size: 1.05rem; color: #475569; }}
        article table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.95rem; }}
        article table th {{ background: #1e293b; color: white; padding: 12px 16px; text-align: left; font-weight: 600; }}
        article table td {{ padding: 10px 16px; border-bottom: 1px solid #e2e8f0; }}
        article table tr:nth-child(even) td {{ background: #f1f5f9; }}
        .pro-list li::before {{ content: '\\2713'; color: #16a34a; font-weight: bold; margin-right: 6px; }}
        .con-list li::before {{ content: '\\2717'; color: #dc2626; font-weight: bold; margin-right: 6px; }}
        .affiliate-disclaimer {{ background: #fef3c7; border-left: 4px solid #f59e0b; padding: 1rem; border-radius: 0.5rem; margin: 1.5rem 0; font-size: 0.9rem; color: #92400e; }}
        .vs-winner {{ color: #16a34a; font-weight: 600; }}
    </style>
{extra_head}
</head>"""


def _html_nav(relative_root: str = "..") -> str:
    return f"""    <nav class="bg-white shadow-sm border-b border-gray-100 sticky top-0 z-50">
        <div class="max-w-4xl mx-auto px-4 py-4 flex items-center justify-between">
            <a href="{relative_root}/index.html" class="text-xl font-extrabold text-slate-900 tracking-tight">Honest<span class="text-orange-500">Gadgets</span></a>
            <div class="flex items-center gap-4">
                <a href="{relative_root}/compare.html" class="text-sm text-slate-500 hover:text-orange-500 transition">Compare</a>
                <a href="{relative_root}/about.html" class="text-sm text-slate-500 hover:text-orange-500 transition">About</a>
                <a href="{relative_root}/index.html" class="text-sm text-slate-500 hover:text-orange-500 transition">← Reviews</a>
            </div>
        </div>
    </nav>"""


def _html_footer(year: int) -> str:
    return f"""    <footer class="bg-slate-900 text-slate-400 text-sm py-8 mt-auto">
        <div class="max-w-4xl mx-auto px-4 text-center">
            &copy; {year} HonestGadgets -- Independent product reviews. All rights reserved.
        </div>
    </footer>"""


# ========================================================================
# 评测 HTML
# ========================================================================
def generate_review_html(slug: str, product: dict, article_content: str) -> Path:
    model = product["model"]
    desc  = f"Honest {model} review -- pros, cons, real user feedback, and expert comparison."
    now   = datetime.now(timezone.utc)
    date_str = now.strftime('%Y-%m-%d')
    keywords = ", ".join(product.get('tags', []))

    extra_head = f"""    <script type="application/ld+json">
    {{
      "@context": "https://schema.org",
      "@type": "Article",
      "headline": "{model} -- In-Depth Honest Review",
      "description": "{desc}",
      "author": {{ "@type": "Person", "name": "Alex Chen", "url": "{SITE_URL}/about.html" }},
      "publisher": {{ "@type": "Organization", "name": "HonestGadgets" }},
      "datePublished": "{date_str}", "dateModified": "{date_str}",
      "mainEntityOfPage": {{ "@type": "WebPage", "@id": "{SITE_URL}/reviews/{slug}.html" }}
    }}
    </script>"""

    html = f"""{_html_head(f"{model} -- In-Depth Review ({now.year}) | HonestGadgets", desc, keywords, extra_head)}
<body class="min-h-screen flex flex-col">
{_html_nav("..")}
    <main class="flex-1 max-w-3xl mx-auto px-4 py-8">
        <div class="affiliate-disclaimer">
            <strong>Affiliate Disclosure:</strong> As an Amazon Associate, we earn from qualifying purchases.
        </div>
        <article>
            {article_content}
        </article>
        <div class="mt-12 pt-8 border-t border-gray-200">
            <a href="../index.html" class="text-orange-500 hover:text-orange-600 font-medium transition">← See All Reviews</a>
        </div>
    </main>
{_html_footer(now.year)}
</body>
</html>"""

    out = REVIEWS_DIR / f"{slug}.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"评测文章已生成: {out}")
    return out


# ========================================================================
# 对比页 HTML
# ========================================================================
def generate_comparison_html(slug: str, a: dict, b: dict, article_content: str) -> Path:
    model_a, model_b = a["model"], b["model"]
    desc = f"{model_a} vs {model_b}: honest head-to-head comparison with real user feedback, specs breakdown, and buying advice."
    now  = datetime.now(timezone.utc)
    date_str = now.strftime('%Y-%m-%d')
    keywords = f"{model_a} vs {model_b}, {model_a} comparison, {model_b} comparison, best {a['name'].lower()}"

    extra_head = f"""    <script type="application/ld+json">
    {{
      "@context": "https://schema.org",
      "@type": "Article",
      "headline": "{model_a} vs {model_b} -- Honest Comparison",
      "description": "{desc}",
      "author": {{ "@type": "Person", "name": "Alex Chen", "url": "{SITE_URL}/about.html" }},
      "publisher": {{ "@type": "Organization", "name": "HonestGadgets" }},
      "datePublished": "{date_str}", "dateModified": "{date_str}",
      "mainEntityOfPage": {{ "@type": "WebPage", "@id": "{SITE_URL}/compare/{slug}.html" }}
    }}
    </script>"""

    html = f"""{_html_head(f"{model_a} vs {model_b} -- Which One Should You Buy? | HonestGadgets", desc, keywords, extra_head)}
<body class="min-h-screen flex flex-col">
{_html_nav("..")}
    <main class="flex-1 max-w-3xl mx-auto px-4 py-8">
        <div class="affiliate-disclaimer">
            <strong>Affiliate Disclosure:</strong> As an Amazon Associate, we earn from qualifying purchases.
        </div>
        <article>
            {article_content}
        </article>
        <div class="mt-12 pt-8 border-t border-gray-200">
            <a href="../compare.html" class="text-orange-500 hover:text-orange-600 font-medium transition">← See All Comparisons</a>
        </div>
    </main>
{_html_footer(now.year)}
</body>
</html>"""

    out = COMPARE_DIR / f"{slug}.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"对比页已生成: {out}")
    return out


# ========================================================================
# 带质量守卫的内容生成流水线
# ========================================================================
def generate_with_quality_gate(
    prompt_fn, product: dict, extra_args: dict = None,
    min_words: int = MIN_WORD_COUNT
) -> Optional[str]:
    """
    统一的"生成 → 质量检查 → 重试"流水线。
    extra_args: 传给 quality validator 的额外参数 (如对比页的第二个商品)。
    """
    prompt = prompt_fn(product, **extra_args) if extra_args else prompt_fn(product)
    content = call_deepseek_api(prompt, max_tokens=4000)
    if content is None:
        return None

    for qr in range(QUALITY_RETRIES + 1):
        if extra_args and "product_b" in extra_args:
            passed, reason = validate_compare_quality(content, product, extra_args["product_b"])
        else:
            passed, reason = validate_content_quality(content, product, min_words)
        if passed:
            logger.info(f"  ✅ 质量检查通过 ({len(content.split())} 词)")
            return content
        if qr < QUALITY_RETRIES:
            logger.warning(f"  ⚠️ 质量不合格 ({reason})，重试 ({qr+1}/{QUALITY_RETRIES})...")
            content = call_deepseek_api(prompt, max_tokens=4000)
            if content is None:
                return None
        else:
            logger.error(f"  ❌ {QUALITY_RETRIES} 次质量重试后仍不合格: {reason}")
            return None  # 质量不合格，跳过发布
    return content


# ========================================================================
# 首页更新
# ========================================================================
def update_index_html(products: list[dict]):
    existing = [p for p in products if (REVIEWS_DIR / f"{p['slug']}.html").exists()]
    now = datetime.now(timezone.utc)

    cards = ""
    for p in existing:
        tags_html = " ".join([
            f'<span class="bg-slate-100 text-slate-600 text-xs px-2 py-1 rounded-full">{t}</span>'
            for t in p.get("tags", [])[:4]
        ])
        cards += f"""
        <div class="bg-white rounded-2xl shadow-sm border border-gray-100 hover:shadow-lg hover:border-orange-200 transition-all duration-300 group overflow-hidden">
            <div class="p-6">
                <div class="flex items-center gap-2 mb-3 flex-wrap">{tags_html}</div>
                <h2 class="text-xl font-bold text-slate-900 mb-2 group-hover:text-orange-500 transition">{p['model']}</h2>
                <p class="text-sm text-slate-500 mb-4">{p['name']} -- In-depth honest review with real user feedback.</p>
                <div class="flex items-center justify-between">
                    <span class="text-2xl font-extrabold text-slate-900">{p['price']}</span>
                    <a href="reviews/{p['slug']}.html" class="inline-flex items-center gap-1 bg-slate-900 text-white px-5 py-2.5 rounded-full text-sm font-semibold hover:bg-orange-500 transition-all duration-300">
                        Read Review <span class="text-lg">→</span>
                    </a>
                </div>
            </div>
        </div>"""

    html = f"""{_html_head("HonestGadgets -- Real Product Reviews, Zero AI-Nonsense", "In-depth product reviews powered by real Amazon user feedback.", "product reviews, honest reviews, gadget reviews")}
<body class="bg-slate-50 min-h-screen flex flex-col">
    <nav class="bg-white shadow-sm border-b border-gray-100">
        <div class="max-w-5xl mx-auto px-4 py-5 flex items-center justify-between">
            <a href="index.html" class="text-2xl font-extrabold text-slate-900 tracking-tight">Honest<span class="text-orange-500">Gadgets</span></a>
            <div class="flex items-center gap-6">
                <a href="compare.html" class="text-sm text-slate-500 hover:text-orange-500 transition">Compare</a>
                <a href="about.html" class="text-sm text-slate-500 hover:text-orange-500 transition">About</a>
                <span class="text-sm text-slate-400">No AI fluff. Just honest reviews.</span>
            </div>
        </div>
    </nav>
    <section class="bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 text-white">
        <div class="max-w-3xl mx-auto px-4 py-16 text-center">
            <h1 class="text-4xl md:text-5xl font-extrabold mb-4 tracking-tight">
                Real Reviews. <span class="text-orange-400">Real Users.</span><br/>Zero ChatGPT-Speak.
            </h1>
            <p class="text-lg text-slate-300 max-w-2xl mx-auto">
                Every review on this site is powered by <strong>real Amazon customer feedback</strong> -- the good, the bad, and the stuff most "expert" reviews conveniently skip.
            </p>
            <div class="mt-6 text-sm text-slate-400">
                Last updated: {now.strftime('%B %d, %Y')} &nbsp;|&nbsp; {len(existing)} in-depth reviews
            </div>
        </div>
    </section>
    <main class="flex-1 max-w-5xl mx-auto px-4 py-10">
        <div class="grid md:grid-cols-2 gap-6">
            {cards if cards else '<div class="col-span-2 text-center py-20 text-slate-400"><p class="text-lg">No reviews generated yet.</p><p class="text-sm mt-2">Run <code>python auto_pilot.py</code> to generate articles.</p></div>'}
        </div>
    </main>
{_html_footer(now.year)}
</body>
</html>"""
    INDEX_FILE.write_text(html, encoding="utf-8")
    logger.info(f"首页已更新 ({len(existing)} 篇评测)")


# ========================================================================
# 对比汇总页
# ========================================================================
def update_compare_index(products: list[dict]):
    """更新 compare.html 对比汇总页"""
    now = datetime.now(timezone.utc)
    existing_pairs = []
    for a, b in category_pairs(products):
        slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
        if (COMPARE_DIR / f"{slug}.html").exists():
            existing_pairs.append((a, b))

    cards = ""
    for a, b in existing_pairs:
        slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
        cards += f"""
        <div class="bg-white rounded-2xl shadow-sm border border-gray-100 hover:shadow-lg hover:border-orange-200 transition-all duration-300 group overflow-hidden">
            <div class="p-6">
                <span class="bg-orange-100 text-orange-700 text-xs px-2 py-1 rounded-full mb-3 inline-block">VS</span>
                <h2 class="text-xl font-bold text-slate-900 mb-2 group-hover:text-orange-500 transition">{a['model']} vs {b['model']}</h2>
                <p class="text-sm text-slate-500 mb-4">Head-to-head comparison: specs, real user feedback, and honest verdict.</p>
                <div class="flex items-center justify-between">
                    <div class="text-sm text-slate-400">{a['price']} vs {b['price']}</div>
                    <a href="compare/{slug}.html" class="inline-flex items-center gap-1 bg-slate-900 text-white px-5 py-2.5 rounded-full text-sm font-semibold hover:bg-orange-500 transition-all duration-300">
                        Compare <span class="text-lg">→</span>
                    </a>
                </div>
            </div>
        </div>"""

    html = f"""{_html_head("Product Comparisons -- Head-to-Head Reviews | HonestGadgets", "Honest side-by-side product comparisons. Specs, real user feedback, and clear recommendations.", "product comparison, vs, head to head")}
<body class="bg-slate-50 min-h-screen flex flex-col">
    <nav class="bg-white shadow-sm border-b border-gray-100">
        <div class="max-w-5xl mx-auto px-4 py-5 flex items-center justify-between">
            <a href="index.html" class="text-2xl font-extrabold text-slate-900 tracking-tight">Honest<span class="text-orange-500">Gadgets</span></a>
            <div class="flex items-center gap-6">
                <a href="compare.html" class="text-sm text-orange-500 font-medium">Compare</a>
                <a href="about.html" class="text-sm text-slate-500 hover:text-orange-500 transition">About</a>
                <a href="index.html" class="text-sm text-slate-500 hover:text-orange-500 transition">Reviews</a>
            </div>
        </div>
    </nav>
    <section class="bg-gradient-to-br from-orange-600 via-red-500 to-orange-700 text-white">
        <div class="max-w-3xl mx-auto px-4 py-12 text-center">
            <h1 class="text-4xl md:text-5xl font-extrabold mb-4 tracking-tight">
                Head-to-Head Comparisons
            </h1>
            <p class="text-lg text-orange-100 max-w-2xl mx-auto">
                Can't decide between two products? Our comparisons break down specs, real user feedback, and hidden downsides -- so you can buy with confidence.
            </p>
            <div class="mt-4 text-sm text-orange-200">
                {len(existing_pairs)} comparisons available
            </div>
        </div>
    </section>
    <main class="flex-1 max-w-5xl mx-auto px-4 py-10">
        <div class="grid md:grid-cols-2 gap-6">
            {cards if cards else '<div class="col-span-2 text-center py-20 text-slate-400"><p class="text-lg">No comparisons generated yet.</p><p class="text-sm mt-2">Run <code>python auto_pilot.py --compare-only</code> to generate comparisons.</p></div>'}
        </div>
    </main>
{_html_footer(now.year)}
</body>
</html>"""
    COMPARE_INDEX.write_text(html, encoding="utf-8")
    logger.info(f"对比汇总页已更新 ({len(existing_pairs)} 篇对比)")


# ========================================================================
# Sitemap
# ========================================================================
def generate_sitemap(products: list[dict], site_url: str):
    urls = [
        f"  <url><loc>{site_url}/index.html</loc><changefreq>daily</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{site_url}/about.html</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>",
        f"  <url><loc>{site_url}/compare.html</loc><changefreq>daily</changefreq><priority>0.8</priority></url>",
    ]
    for p in products:
        if (REVIEWS_DIR / f"{p['slug']}.html").exists():
            urls.append(f"  <url><loc>{site_url}/reviews/{p['slug']}.html</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>")
    for a, b in category_pairs(products):
        slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
        if (COMPARE_DIR / f"{slug}.html").exists():
            urls.append(f"  <url><loc>{site_url}/compare/{slug}.html</loc><changefreq>weekly</changefreq><priority>0.85</priority></url>")

    sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>
"""
    SITEMAP_FILE.write_text(sitemap, encoding="utf-8")
    logger.info(f"sitemap.xml 已更新 ({len(urls)} 个 URL)")


# ========================================================================
# RSS Feed
# ========================================================================
def generate_rss_feed(products: list[dict], site_url: str):
    """生成 RSS 2.0 feed，用于 Bing/Google 发现新内容"""
    now = datetime.now(timezone.utc)
    items = []

    # 评测文章
    for p in products:
        review_path = REVIEWS_DIR / f"{p['slug']}.html"
        if review_path.exists():
            mtime = datetime.fromtimestamp(review_path.stat().st_mtime, tz=timezone.utc)
            items.append(f"""    <item>
      <title>{p['model']} -- In-Depth Review</title>
      <link>{site_url}/reviews/{p['slug']}.html</link>
      <description>Honest {p['model']} review with pros, cons, and real user feedback.</description>
      <pubDate>{mtime.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
      <guid isPermaLink="true">{site_url}/reviews/{p['slug']}.html</guid>
    </item>""")

    # 对比页
    for a, b in category_pairs(products):
        slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
        compare_path = COMPARE_DIR / f"{slug}.html"
        if compare_path.exists():
            mtime = datetime.fromtimestamp(compare_path.stat().st_mtime, tz=timezone.utc)
            items.append(f"""    <item>
      <title>{a['model']} vs {b['model']} -- Comparison</title>
      <link>{site_url}/compare/{slug}.html</link>
      <description>Head-to-head comparison: {a['model']} vs {b['model']}. Specs, real user feedback, and honest verdict.</description>
      <pubDate>{mtime.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
      <guid isPermaLink="true">{site_url}/compare/{slug}.html</guid>
    </item>""")

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>HonestGadgets -- Real Product Reviews</title>
    <link>{site_url}</link>
    <description>In-depth product reviews powered by real Amazon user feedback.</description>
    <language>en-us</language>
    <lastBuildDate>{now.strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>
    <atom:link href="{site_url}/rss.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    RSS_FILE.write_text(rss, encoding="utf-8")
    logger.info(f"rss.xml 已更新 ({len(items)} 个条目)")


# ========================================================================
# Bing IndexNow
# ========================================================================
def submit_to_bing_indexnow(urls: list[str]) -> bool:
    """通过 IndexNow API 提交 URL 到 Bing"""
    key = get_or_create_indexnow_key()
    key_file_url = f"{SITE_URL.rstrip('/')}/indexnow-key.txt"

    payload = json.dumps({
        "host": SITE_URL.split("//")[1].split("/")[0],
        "key": key,
        "keyLocation": key_file_url,
        "urlList": urls
    }).encode("utf-8")

    headers = {"Content-Type": "application/json; charset=utf-8"}

    for attempt in range(2):
        try:
            req = urllib.request.Request(
                "https://api.indexnow.org/indexnow",
                data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status in (200, 202):
                    logger.info(f"IndexNow 提交成功: {len(urls)} 个 URL")
                    return True
                logger.warning(f"IndexNow 返回: {resp.status}")
        except Exception as e:
            if attempt == 0:
                logger.warning(f"IndexNow 提交失败 (尝试 {attempt+1}): {e}，正在重试...")
                time.sleep(5)
            else:
                logger.error(f"IndexNow 提交最终失败: {e}")
    return False


def collect_indexnow_urls(products: list[dict]) -> list[str]:
    """收集需要提交到 Bing 的 URL"""
    urls = [f"{SITE_URL.rstrip('/')}/index.html",
            f"{SITE_URL.rstrip('/')}/compare.html",
            f"{SITE_URL.rstrip('/')}/rss.xml"]
    for p in products:
        rp = REVIEWS_DIR / f"{p['slug']}.html"
        if rp.exists():
            urls.append(f"{SITE_URL.rstrip('/')}/reviews/{p['slug']}.html")
    for a, b in category_pairs(products):
        slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
        cp = COMPARE_DIR / f"{slug}.html"
        if cp.exists():
            urls.append(f"{SITE_URL.rstrip('/')}/compare/{slug}.html")
    return urls


# ========================================================================
# 健康报告
# ========================================================================
def generate_health_report(start_time, s_ok, s_fail, s_skip, s_failed_list,
                           c_ok, c_fail, c_skip, c_failed_list,
                           balance, total_products):
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    total_ok = s_ok + c_ok
    total_fail = s_fail + c_fail
    balance_str = f"${balance:.2f}" if balance else "Unknown"

    if total_fail == 0:
        status = "✅ HEALTHY"
    elif total_fail < total_products:
        status = "⚠️ DEGRADED"
    else:
        status = "🔴 CRITICAL"

    sections = [
        f"# AutoPilot Health Report v3.0",
        f"",
        f"**Status:** {status}",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Runtime:** {elapsed:.1f}s",
        f"",
        f"## Review Generation",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Success | {s_ok} |",
        f"| Skipped | {s_skip} |",
        f"| Failed  | {s_fail} |",
        f"",
        f"## Comparison Generation",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Success | {c_ok} |",
        f"| Skipped | {c_skip} |",
        f"| Failed  | {c_fail} |",
        f"",
        f"## System",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| API Balance | {balance_str} |",
        f"| Total Products | {total_products} |",
        f"| Site URL | {SITE_URL} |",
        f"",
    ]

    if total_fail > 0:
        sections.append("## Action Items")
        sections.append("")
        if s_failed_list:
            sections.append("- [ ] Investigate failed reviews: " + ", ".join(s_failed_list))
        if c_failed_list:
            sections.append("- [ ] Investigate failed comparisons: " + ", ".join(c_failed_list))
        sections.append("")

    if balance and balance < BALANCE_WARN_THRESHOLD:
        sections.append(f"- [ ] **Recharge API balance** (currently {balance_str})")
        sections.append("")

    if not PUSH_DEER_KEY and not SERVER_CHAN_KEY:
        sections.append("- [ ] Configure phone alerts (PUSH_DEER_KEY or SERVER_CHAN_KEY)")
        sections.append("")

    sections.append("---")
    sections.append("*Report auto-generated by AutoPilot v3.0*")

    report = "\n".join(sections)
    HEALTH_FILE.write_text(report, encoding="utf-8")
    logger.info(f"健康报告已生成: {HEALTH_FILE}")
    return report


# ========================================================================
# 主流程
# ========================================================================
def main():
    logger.info("=" * 60)
    logger.info("AI 联盟营销评测网站 -- AutoPilot v3.0 启动")
    logger.info(f"模型: {DEEPSEEK_MODEL_ID}")
    logger.info(f"模式: {'REVIEWS-ONLY' if MODE_REVIEWS_ONLY else 'COMPARE-ONLY' if MODE_COMPARE_ONLY else 'DRY-RUN' if MODE_DRY_RUN else 'FULL'}")
    logger.info("=" * 60)

    # ---- 校验 API Key ----
    if not DEEPSEEK_API_KEY and not MODE_DRY_RUN:
        msg = "DEEPSEEK_API_KEY 未配置！"
        logger.critical(msg)
        send_phone_alert("AutoPilot 启动失败", msg)
        return

    # ---- 预检余额 ----
    balance = check_api_balance()
    if balance is not None:
        if balance < BALANCE_CRITICAL_THRESHOLD:
            msg = f"API 余额严重不足: ${balance:.2f} (< ${BALANCE_CRITICAL_THRESHOLD})"
            logger.critical(msg)
            send_phone_alert("🔴 AutoPilot 余额告急", msg + "\n脚本已中止。")
            return
        elif balance < BALANCE_WARN_THRESHOLD:
            logger.warning(f"API 余额偏低: ${balance:.2f}")
            send_phone_alert("⚠️ AutoPilot 余额预警", f"余额 ${balance:.2f}，建议充值。")

    # ---- 读取商品数据 ----
    try:
        products = json.loads(PRODUCTS.read_text(encoding="utf-8"))
        logger.info(f"已读取 {len(products)} 个商品")
    except Exception as e:
        logger.critical(f"无法读取 products.json: {e}")
        send_phone_alert("AutoPilot 致命错误", str(e))
        return

    start_time = datetime.now(timezone.utc)
    pair_count = len(list(category_pairs(products)))  # C(n,2)

    # Cost estimation
    price_in = 0.27 / 1_000_000
    price_out = 1.10 / 1_000_000
    est_review = len(products) * (2000 * price_in + 3500 * price_out)
    est_compare = pair_count * (3000 * price_in + 4000 * price_out)
    logger.info(f'Cost estimate: ${est_review:.2f} reviews + ${est_compare:.2f} compare = ${est_review + est_compare:.2f} total')
    logger.info(f'Pages: {len(products)} reviews + {pair_count} compare = {len(products) + pair_count} total')

    # ================================================================
    # Phase 1: 评测生成
    # ================================================================
    s_ok, s_fail, s_skip = 0, 0, 0
    s_failed_list = []

    if not MODE_COMPARE_ONLY:
        logger.info(f"\n{'#' * 50}")
        logger.info(f"Phase 1/3: 评测生成 ({len(products)} 个商品)")
        logger.info(f"{'#' * 50}")

        for i, p in enumerate(products, 1):
            slug, model = p["slug"], p["model"]
            logger.info(f"\n[{i}/{len(products)}] {model}")

            existing = REVIEWS_DIR / f"{slug}.html"
            if existing.exists():
                logger.info(f"  → 已存在，跳过")
                s_skip += 1
                continue

            content = generate_with_quality_gate(build_review_prompt, p)
            if content is None:
                s_fail += 1
                s_failed_list.append(model)
                send_phone_alert(f"AutoPilot 生成失败: {model}",
                                 f"{MAX_RETRIES} 次重试后仍失败，已跳过。")
                continue

            try:
                generate_review_html(slug, p, content)
                s_ok += 1
            except Exception as e:
                s_fail += 1
                s_failed_list.append(model)
                logger.error(f"HTML 生成失败: {e}")

    # ================================================================
    # Phase 2: 对比页生成
    # ================================================================
    c_ok, c_fail, c_skip = 0, 0, 0
    c_failed_list = []

    if not MODE_REVIEWS_ONLY:
        logger.info(f"\n{'#' * 50}")
        logger.info(f"Phase 2/3: 对比页生成 ({pair_count} 组对比)")
        logger.info(f"{'#' * 50}")

        pair_idx = 0
        for a, b in category_pairs(products):
            pair_idx += 1
            slug = f"{a['slug'].replace('-review','')}-vs-{b['slug'].replace('-review','')}"
            label = f"{a['model']} vs {b['model']}"
            logger.info(f"\n[{pair_idx}/{pair_count}] {label}")

            existing = COMPARE_DIR / f"{slug}.html"
            if existing.exists():
                logger.info(f"  → 已存在，跳过")
                c_skip += 1
                continue

            # 对比页：直接调用（需要两个商品，不走 generate_with_quality_gate）
            prompt = build_comparison_prompt(a, b)
            content = call_deepseek_api(prompt, max_tokens=4000)
            if content is not None:
                # 质量守卫
                for qr in range(QUALITY_RETRIES + 1):
                    passed, reason = validate_compare_quality(content, a, b)
                    if passed:
                        logger.info(f"  ✅ 质量检查通过 ({len(content.split())} 词)")
                        break
                    if qr < QUALITY_RETRIES:
                        logger.warning(f"  ⚠️ 质量不合格 ({reason})，重试 ({qr+1}/{QUALITY_RETRIES})...")
                        content = call_deepseek_api(prompt, max_tokens=4000)
                        if content is None:
                            break
                    else:
                        logger.error(f"  ❌ {QUALITY_RETRIES} 次质量重试后仍不合格: {reason}")
                        content = None  # 质量不合格，跳过发布
            if content is None:
                c_fail += 1
                c_failed_list.append(label)
                send_phone_alert(f"对比生成失败: {label}",
                                 f"{MAX_RETRIES} 次重试后仍失败，已跳过。")
                continue

            try:
                generate_comparison_html(slug, a, b, content)
                c_ok += 1
            except Exception as e:
                c_fail += 1
                c_failed_list.append(label)
                logger.error(f"对比 HTML 生成失败: {e}")

    # ================================================================
    # Phase 3: 站点基建更新
    # ================================================================
    logger.info(f"\n{'#' * 50}")
    logger.info(f"Phase 3/3: 站点基建更新")
    logger.info(f"{'#' * 50}")

    update_index_html(products)
    update_compare_index(products)
    generate_sitemap(products, SITE_URL)
    generate_rss_feed(products, SITE_URL)

    # Bing IndexNow
    indexnow_urls = collect_indexnow_urls(products)
    if indexnow_urls and not MODE_DRY_RUN:
        submit_to_bing_indexnow(indexnow_urls)
    elif MODE_DRY_RUN:
        logger.info("[DRY-RUN] 跳过 IndexNow 提交")

    # 健康报告
    generate_health_report(start_time, s_ok, s_fail, s_skip, s_failed_list,
                           c_ok, c_fail, c_skip, c_failed_list,
                           balance, len(products))

    # ---- 收尾 ----
    total_s = s_ok + s_fail + s_skip
    total_c = c_ok + c_fail + c_skip
    summary = (
        f"\nAutoPilot v3.0 执行完成\n"
        f"{'=' * 40}\n"
        f"评测: {s_ok} 成功 / {s_skip} 跳过 / {s_fail} 失败 (共 {len(products)} 商品)\n"
        f"对比: {c_ok} 成功 / {c_skip} 跳过 / {c_fail} 失败 (共 {pair_count} 组对比)\n"
        f"IndexNow: 已提交 {len(indexnow_urls)} 个 URL\n"
    )
    logger.info(summary)

    total_failures = s_fail + c_fail
    if total_failures > 0:
        all_failed = s_failed_list + c_failed_list
        send_phone_alert(f"AutoPilot 执行完毕 ({total_failures} 项失败)", summary)

    logger.info("脚本结束。")


if __name__ == "__main__":
    main()
