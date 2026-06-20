#!/usr/bin/env python3
"""Daily Brief Generator - 日报生成器
Usage: python generate_brief.py --type [finance|ai|ai_apps]
"""
import argparse, json, os, re, time
from datetime import datetime, timedelta
from html import escape

try:
    import feedparser
    import requests
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "feedparser", "-q"])
    import feedparser
    import requests

# ── Configuration ──────────────────────────────────────

CACHE_FILE = "scripts/.news_cache.json"
CACHE_HOURS = 6

# RSS feeds by category
FEEDS = {
    "finance": [
        # 财经
        ("https://rsshub.app/cls/telegraph", "财联社电报"),
        ("http://feedmaker.kindle4rss.com/feeds/cls-finance.xml", "财联社"),
        ("https://rsshub.app/36kr/motif/7", "36氪-金融"),
        ("https://rsshub.app/eastmoney/search/web+金融科技", "东方财富"),
        ("https://rsshub.app/sina/finance", "新浪财经"),
    ],
    "ai": [
        ("https://36kr.com/feed", "36氪"),
        ("https://rsshub.app/36kr/motif/8", "36氪-AI"),
        ("https://rsshub.app/jike/topic/人工智能", "即刻-AI"),
        ("https://rsshub.app/ithome/it", "IT之家"),
        ("https://rsshub.app/zhihu/daily", "知乎日报"),
    ],
    "ai_apps": [
        ("https://36kr.com/feed", "36氪"),
        ("https://rsshub.app/sspai", "少数派"),
        ("https://rsshub.app/geekpark/breakingnews", "极客公园"),
        ("https://rsshub.app/ithome/it", "IT之家"),
        ("https://rsshub.app/jike/topic/AI工具", "即刻-AI工具"),
    ],
}

# Category keywords for filtering and sorting
CATEGORIES = {
    "finance": {
        "宏观政策": ["政策", "央行", "证监会", "利率", "降息", "加息", "美联储", "国务院", "发改委", "财政部", "监管", "数据", "经济"],
        "金融科技动态": ["金融科技", "科技金融", "数字", "区块链", "支付", "银行", "保险", "金融", "信贷", "贷款"],
        "资本市场表现": ["A股", "港股", "美股", "股票", "指数", "股市", "大盘", "行情", "跌", "涨", "ETF", "IPO"],
        "投融资事件": ["融资", "投资", "收购", "并购", "投融资", "估值", "VC", "PE", "天使", "A轮", "B轮", "上市"],
    },
    "ai": {
        "AI 产业政策与监管": ["政策", "监管", "立法", "法规", "标准", "伦理", "政府", "国家", "规划"],
        "大模型最新进展": ["大模型", "LLM", "模型", "参数", "开源", "训练", "推理", "GPT", "Claude", "Gemini", "智谱", "百度", "阿里"],
        "AI 应用与商业化": ["应用", "商业化", "落地", "产品", "发布", "推出", "上线", "服务", "客户", "收入"],
        "投融资与资本动态": ["融资", "投资", "估值", "收购", "上市", "VC", "PE", "IPO", "亿"],
    },
    "ai_apps": {
        "AI 工具与产品发布": ["工具", "产品", "发布", "推出", "上线", "更新", "新功能", "插件", "扩展"],
        "AI + 行业落地案例": ["落地", "案例", "行业", "医疗", "教育", "金融", "制造", "零售", "办公", "编程", "驾驶"],
        "AI Agent 与智能体": ["Agent", "智能体", "自主", "代理", "编排", "协作", "自动化", "编排"],
        "AI 创业与商业模式": ["创业", "商业模式", "订阅", "SaaS", "收费", "定价", "营收", "增长"],
    },
}

# Brief colors by type
BRIEF_CONFIG = {
    "finance": {"title": "📊 财经简报", "subtitle": "科技金融 · A股港股 · 美联储 · 一级市场", "color": "#1a1a2e", "accent": "#e74c3c", "gradient": "#1a1a2e, #16213e"},
    "ai": {"title": "🤖 AI 产业简报", "subtitle": "大模型 · 智能体 · 具身智能 · 投融资", "color": "#6c5ce7", "accent": "#6c5ce7", "gradient": "#6c5ce7, #4a3cd4"},
    "ai_apps": {"title": "🚀 AI 应用简报", "subtitle": "AI 工具 · 行业落地 · Agent · 商业模式", "color": "#27ae60", "accent": "#27ae60", "gradient": "#27ae60, #1e8449"},
}

# ── Helper Functions ───────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < CACHE_HOURS * 3600:
                return data.get("items", [])
        except: pass
    return []

def save_cache(items):
    with open(CACHE_FILE, "w") as f:
        json.dump({"ts": time.time(), "items": items}, f)

def fetch_news(brief_type):
    """Fetch and deduplicate news from RSS feeds."""
    cached = load_cache()
    if cached:
        return cached

    all_items = []
    seen_titles = set()

    for url, source_name in FEEDS.get(brief_type, []):
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                if len(title) < 4 or title in seen_titles:
                    continue
                seen_titles.add(title)

                summary = entry.get("summary", "") or entry.get("description", "")
                summary = re.sub(r"<[^>]+>", "", summary)
                summary = re.sub(r"\s+", " ", summary).strip()
                if len(summary) > 200:
                    summary = summary[:200] + "..."

                link = entry.get("link", "")
                published = entry.get("published", "") or entry.get("updated", "")

                all_items.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source_name,
                    "date": published,
                })
        except Exception as e:
            print(f"  ⚠ {url}: {e}")

    save_cache(all_items)
    return all_items


def categorize(items, brief_type):
    """Sort items into categories based on keyword matching."""
    cat_config = CATEGORIES.get(brief_type, {})
    categorized = {cat: [] for cat in cat_config}
    uncategorized = []

    for item in items:
        title = item["title"]
        matched = False
        for cat, keywords in cat_config.items():
            if any(kw in title for kw in keywords):
                categorized[cat].append(item)
                matched = True
                break
        if not matched:
            uncategorized.append(item)

    # Trim to 2 items per category, then fill with uncategorized
    for cat in categorized:
        categorized[cat] = categorized[cat][:2]

    all_assigned = sum(len(v) for v in categorized.values())
    needed = 7 - all_assigned
    for item in uncategorized:
        if needed <= 0:
            break
        # Find category with fewest items
        min_cat = min(categorized, key=lambda c: len(categorized[c]))
        categorized[min_cat].append(item)
        needed -= 1

    return categorized


def use_gemini(items, brief_type):
    """Use Gemini API to generate better summaries."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    config = BRIEF_CONFIG.get(brief_type, {})
    prompt = f"你是一个{brief_type}资讯编辑。请根据以下新闻标题和原始摘要，为每条生成一句50字以内的中文核心摘要。\n\n"
    for i, item in enumerate(items[:12]):
        prompt += f"{i+1}. 标题：{item['title']}\n   原文：{item['summary'][:100]}\n\n"
    prompt += "\n请以JSON数组格式返回：[{\"index\": 1, \"summary\": \"...\"}, ...]"

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Extract JSON
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"  Gemini error: {e}")
    return None


def build_html(brief_type, categorized, gemini_summaries=None):
    """Generate the full HTML page."""
    config = BRIEF_CONFIG.get(brief_type, {})
    cat_config = CATEGORIES.get(brief_type, {})
    today = datetime.now().strftime("%Y年%m月%d日")
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # Summary grid colors
    grid_colors = ["red", "blue", "green", "orange"]
    cat_keys = list(cat_config.keys())
    icon_map = {
        "宏观政策": ("🏛️", "policy"),
        "金融科技动态": ("💳", "policy"),
        "资本市场表现": ("📈", "market"),
        "投融资事件": ("💰", "pe"),
        "AI 产业政策与监管": ("📜", "policy"),
        "大模型最新进展": ("🧠", "model"),
        "AI 应用与商业化": ("🔧", "app"),
        "投融资与资本动态": ("💰", "capital"),
        "AI 工具与产品发布": ("🛠️", "tool"),
        "AI + 行业落地案例": ("🏭", "case"),
        "AI Agent 与智能体": ("🤖", "agent"),
        "AI 创业与商业模式": ("💡", "biz"),
    }

    # Build summary grid
    summary_items = ""
    for i, (cat, items) in enumerate(categorized.items()):
        color = grid_colors[i % 4]
        summary_items += f'<div class="summary-item {color}"><div class="summary-num">{len(items)}</div><div class="summary-label">{cat}</div></div>'

    # Build sections
    sections = ""
    idx = 0
    for cat, items in categorized.items():
        if not items:
            continue
        icon_name, icon_class = icon_map.get(cat, ("📌", "tool"))
        section_items = ""
        for item in items:
            idx += 1
            idx_cls = "active" if idx <= 2 else ""
            summary = item.get("summary", "")
            if len(summary) > 50:
                summary = summary[:50] + "..."
            section_items += f"""
    <div class="news-item">
      <div class="news-index {idx_cls}">{idx}</div>
      <div class="news-body">
        <div class="news-title">{escape(item['title'])}</div>
        <div class="news-summary">{escape(summary)}</div>
        <div class="news-meta">
          <span class="news-source">{escape(item['source'])}</span>
          <a class="news-link" href="{escape(item['link'])}">查看原文</a>
        </div>
      </div>
    </div>"""

        sections += f"""
  <div class="section">
    <div class="section-header">
      <div class="section-icon {icon_class}">{icon_name}</div>
      <div class="section-name">{cat}</div>
      <div class="section-count">{len(items)} 条</div>
    </div>{section_items}
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>{config['title']} · {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;background:#f5f6fa;color:#1a1a2e;line-height:1.7;padding:16px}}
.container{{max-width:420px;margin:0 auto}}
.header{{background:linear-gradient(135deg,{config['gradient']});border-radius:16px;padding:24px 20px 20px;color:#fff;margin-bottom:16px;position:relative;overflow:hidden}}
.header::after{{content:"";position:absolute;top:-30px;right:-30px;width:120px;height:120px;background:rgba(255,255,255,0.05);border-radius:50%}}
.header-date{{font-size:12px;color:rgba(255,255,255,0.6);letter-spacing:1px;margin-bottom:6px}}
.header-title{{font-size:22px;font-weight:700;letter-spacing:1px}}
.header-sub{{font-size:13px;color:rgba(255,255,255,0.55);margin-top:6px}}
.header-tag{{display:inline-block;background:rgba(255,255,255,0.15);border-radius:20px;padding:3px 10px;font-size:11px;color:rgba(255,255,255,0.8);margin-top:10px}}
.summary-card{{background:#fff;border-radius:14px;padding:16px 18px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,0.05)}}
.summary-title{{font-size:14px;font-weight:600;color:#1a1a2e;margin-bottom:12px;display:flex;align-items:center;gap:6px}}
.summary-title .dot{{width:6px;height:6px;border-radius:50%;background:{config['accent']};flex-shrink:0}}
.summary-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.summary-item{{background:#f8f9fc;border-radius:10px;padding:12px;text-align:center}}
.summary-num{{font-size:22px;font-weight:700;color:#1a1a2e}}
.summary-label{{font-size:11px;color:#888;margin-top:2px}}
.summary-item.red .summary-num{{color:#e74c3c}}
.summary-item.blue .summary-num{{color:#2980b9}}
.summary-item.green .summary-num{{color:#27ae60}}
.summary-item.orange .summary-num{{color:#e67e22}}
.section{{background:#fff;border-radius:14px;margin-bottom:14px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.05)}}
.section-header{{padding:14px 18px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid #f0f0f5}}
.section-icon{{width:32px;height:32px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}}
.section-icon.policy{{background:#fff3e0}}
.section-icon.market{{background:#e3f2fd}}
.section-icon.pe{{background:#fce4ec}}
.section-icon.model{{background:#ede7f6}}
.section-icon.app{{background:#e8f5e9}}
.section-icon.capital{{background:#fce4ec}}
.section-icon.tool{{background:#e8f5e9}}
.section-icon.case{{background:#e3f2fd}}
.section-icon.agent{{background:#ede7f6}}
.section-icon.biz{{background:#fff3e0}}
.section-name{{font-size:15px;font-weight:700;color:#1a1a2e}}
.section-count{{margin-left:auto;font-size:12px;color:#aaa}}
.news-item{{padding:14px 18px;border-bottom:1px solid #f5f5fa;position:relative}}
.news-item:last-child{{border-bottom:none}}
.news-index{{position:absolute;left:18px;top:14px;width:20px;height:20px;border-radius:6px;background:#f0f0f5;font-size:11px;font-weight:700;color:#888;display:flex;align-items:center;justify-content:center}}
.news-index.active{{background:{config['accent']};color:#fff}}
.news-body{{padding-left:30px}}
.news-title{{font-size:14px;font-weight:600;color:#1a1a2e;line-height:1.5;margin-bottom:6px}}
.news-summary{{font-size:12.5px;color:#666;line-height:1.6;margin-bottom:8px}}
.news-meta{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.news-source{{font-size:11px;color:{config['accent']};background:rgba({','.join(str(int(c)) for c in [i for i in bytes.fromhex(config['accent'].lstrip('#') + config['accent'].lstrip('#'))])},0.1);border-radius:4px;padding:2px 7px}}
.news-link{{font-size:11px;color:#aaa;text-decoration:none;word-break:break-all}}
.news-link:hover{{color:{config['accent']}}}
.footer{{text-align:center;padding:20px 0 30px;font-size:11px;color:#bbb}}
.footer span{{color:{config['accent']};font-weight:600}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<div class="header-date">{today}</div>
<div class="header-title">{config['title']}</div>
<div class="header-sub">{config['subtitle']}</div>
<div class="header-tag">由 WorkBuddy AI · GitHub Actions 自动整理</div>
</div>
<div class="summary-card">
<div class="summary-title"><span class="dot"></span> 今日概览</div>
<div class="summary-grid">{summary_items}</div>
</div>
{sections}
<div class="footer">由 <span>WorkBuddy AI</span> 自动整理 · 仅供参考<br>{now_str} 更新</div>
</div>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, choices=["finance", "ai", "ai_apps"])
    args = parser.parse_args()

    bt = args.type
    file_map = {"finance": "finance.html", "ai": "ai.html", "ai_apps": "ai-apps.html"}
    output_file = file_map[bt]
    config = BRIEF_CONFIG[bt]

    print(f"🔍 正在生成 {config['title']}...")

    # Fetch
    items = fetch_news(bt)
    print(f"  ✅ 获取 {len(items)} 条新闻")

    # Categorize
    categorized = categorize(items, bt)
    total = sum(len(v) for v in categorized.values())
    print(f"  ✅ 分类完成，共 {total} 条")

    # Optional Gemini
    gemini = use_gemini(items, bt)
    if gemini:
        print(f"  ✅ Gemini 摘要生成完成")

    # Build HTML
    html = build_html(bt, categorized, gemini)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✅ 已生成 {output_file}")


if __name__ == "__main__":
    main()
