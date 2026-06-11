#!/usr/bin/env python3
"""
自动选品脚本 — 每周运行，从品类中发现新品并自动加入产品库

流程:
  1. 读取 categories.json (目标品类)
  2. 读取 products.json (已有产品，避免重复)
  3. 对每个品类，调用 DeepSeek 发现热门产品
  4. 解析 JSON → 验证 Amazon 链接 → 去重 → 入库
  5. 报告新增产品

品类上限: 每个品类最多维持 8 个产品 (避免对比页爆炸)
           每月轮换一次 (旧产品标记为 inactive，新品类进来)
"""
import os, sys, json, time, logging, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

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
                    os.environ[k] = value

DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL_ID = os.getenv("DEEPSEEK_MODEL_ID", "deepseek-v4-pro")
AFFILIATE_TAG     = os.getenv("AFFILIATE_TAG", "")

BASE_DIR      = Path(__file__).parent
CATEGORIES    = BASE_DIR / "categories.json"
PRODUCTS_FILE = BASE_DIR / "products.json"
LOG_FILE      = BASE_DIR / "sourcer.log"

MAX_PER_CATEGORY = 8      # 每品类最多维持产品数
MIN_PER_CATEGORY = 3      # 每品类最少产品数 (不够就补)
MAX_NEW_PER_RUN  = 30     # 单次运行最多新增产品数 (控制成本)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def call_deepseek(prompt: str) -> Optional[str]:
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = json.dumps({
        "model": DEEPSEEK_MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3000, "temperature": 0.3, "stream": False
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"API 调用失败: {e}")
        return None


def validate_product(p: dict) -> bool:
    """Validate product: URL format + basic field integrity (no HEAD request from China)"""
    url = p.get("amazon_url", "")
    if not url or "amazon.com/dp/" not in url:
        return False
    model = p.get("model", "")
    price = p.get("price", "")
    if not model or not price:
        return False
    # Format check: ASIN = B + 10 alphanumeric chars
    asin = url.split("/dp/")[-1].split("?")[0].split("/")[0]
    if not asin or not asin.startswith("B") or len(asin) < 10:
        return False
    return True


def slugify(text: str) -> str:
    return text.lower().replace(" ", "-").replace("(", "").replace(")", "").replace("+", "plus")[:60]


def discover_products(category: dict, existing_models: set, needed: int) -> list[dict]:
    """调用 DeepSeek 发现品类下的热门产品"""
    prompt = f"""You are an Amazon product researcher. List {needed+5} real, currently popular products in "{category['name']}" sold on Amazon.com.

Return ONLY a JSON array (no markdown, no explanation). Each object must have:
{{
  "name": "{category['name']}",
  "model": "Exact product model name",
  "price": "$XX.XX",
  "amazon_url": "https://www.amazon.com/dp/REAL_ASIN",
  "specs": {{"Key Spec 1": "value", "Key Spec 2": "value", ...}} (5-7 realistic specs),
  "tags": ["keyword1", "keyword2", "keyword3", "keyword4"] (SEO keywords)
}}

Rules:
- Only list products that definitely exist on Amazon in 2025
- Use realistic, verifiable model names (not placeholders)
- Do NOT include these already-known models: {", ".join(sorted(existing_models)[:20])}
- Prioritize products with many reviews and strong sales rank
- Vary price points (include budget, mid-range, and premium)"""

    content = call_deepseek(prompt)
    if not content:
        return []

    # 提取 JSON
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("\n```", 1)[0]

    try:
        candidates = json.loads(content)
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list):
            logger.error(f"非预期的响应格式: {type(candidates)}")
            return []
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        return []

    results = []
    for c in candidates:
        model = c.get("model", "")
        if not model or model in existing_models:
            continue
        if len(results) >= needed:
            break

        c["category"] = category["slug"]
        c["slug"] = slugify(model) + "-review"
        c["added"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c["status"] = "active"
        c["amazon_url"] = c.get("amazon_url", "") + AFFILIATE_TAG

        # 验证
        if validate_product(c):
            results.append(c)
            existing_models.add(model)
            logger.info(f"  ✅ {model} ({c.get('price', 'N/A')})")
        else:
            logger.info(f"  ⚠️ {model}: Amazon 验证失败，跳过")

    return results


def main():
    logger.info("=" * 50)
    logger.info("AutoSourcer — 自动选品脚本启动")

    if not DEEPSEEK_API_KEY:
        logger.critical("DEEPSEEK_API_KEY 未配置")
        return

    # 读取品类
    try:
        categories = json.loads(CATEGORIES.read_text(encoding="utf-8"))
        logger.info(f"已加载 {len(categories)} 个品类")
    except Exception as e:
        logger.critical(f"categories.json 读取失败: {e}")
        return

    # 读取已有产品
    try:
        products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        products = []

    # 按品类分组
    by_category = {}
    for p in products:
        cat = p.get("category", "uncategorized")
        by_category.setdefault(cat, []).append(p)

    total_added = 0

    for cat in categories:
        cat_slug = cat["slug"]
        existing = by_category.get(cat_slug, [])
        active = [p for p in existing if p.get("status") != "inactive"]
        current_count = len(active)

        if current_count >= MAX_PER_CATEGORY:
            logger.info(f"\n{cat['name']}: 已有 {current_count} 个产品 (上限 {MAX_PER_CATEGORY})，跳过")
            continue

        needed = min(MAX_PER_CATEGORY - current_count, MAX_NEW_PER_RUN - total_added)
        if needed <= 0:
            logger.info(f"已达单次上限 {MAX_NEW_PER_RUN}，停止")
            break

        logger.info(f"\n{cat['name']}: {current_count} → 目标 {MAX_PER_CATEGORY}，需要 +{needed}")
        existing_models = {p["model"] for p in existing}
        new_products = discover_products(cat, existing_models, needed)

        for np in new_products:
            products.append(np)
            total_added += 1

        if new_products:
            logger.info(f"  → 新增 {len(new_products)} 个产品")
        else:
            logger.info(f"  → 未发现新产品")

        time.sleep(2)  # API 限流

    # 保存
    if total_added > 0:
        PRODUCTS_FILE.write_text(json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"\n{'=' * 50}")
        logger.info(f"✅ 共新增 {total_added} 个产品")
        logger.info(f"当前产品库: {len(products)} 个")
    else:
        logger.info("\n未新增任何产品 (所有品类已达上限或发现失败)")

    logger.info(f"下次建议运行: python auto_pilot.py")


if __name__ == "__main__":
    main()
