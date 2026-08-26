#!/usr/bin/env python3
"""Daily Brief Generator - 日报生成器
Usage: python generate_brief.py --type [finance|ai|ai_apps|newenergy|entertainment|semiconductor]
"""
import argparse, json, os, re, sys, time, io, base64, concurrent.futures as cf
import urllib.parse
from datetime import datetime, timedelta
from html import escape

# 防缓存自动重定向脚本（普通字符串，避免 f-string 误解析 JS 中的 {}）
REDIRECT_SCRIPT = """
<script>
(function(){
  var d = new Date();
  var p = function(n){ return (n < 10 ? '0' : '') + n; };
  var v = '' + d.getFullYear() + p(d.getMonth()+1) + p(d.getDate());
  var u = new URL(location.href);
  if (u.searchParams.get('v') !== v) { u.searchParams.set('v', v); location.replace(u.toString()); }
})();
</script>
"""

try:
    import feedparser
    import requests
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "feedparser", "-q"])
    import feedparser
    import requests

# ── Configuration ──────────────────────────────────────

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
# 主数据源：Google News 中文 RSS（云端/美国环境稳定可达，本地被墙属正常）
GOOGLE_QUERIES = {
    "finance": ["财经 A股 股市", "美联储 降息", "港股 美股 行情"],
    "ai": ["人工智能 大模型", "AI 智能体 agent", "ChatGPT 发布 融资"],
    "ai_apps": ["AI 应用 工具", "AI Agent 创业", "AI 产品 发布"],
    "newenergy": ["新能源 光伏 储能", "电动车 电池 比亚迪", "碳中和 风电"],
    "entertainment": ["电影 票房 上映", "综艺 热播", "游戏 电竞", "明星 官宣"],
    "semiconductor": ["半导体 芯片", "晶圆 光刻机", "GPU 英伟达 存储", "中芯国际 代工"],
}

CACHE_FILE = "scripts/.news_cache.json"
CACHE_HOURS = 6

# RSS feeds by category
# RSS feeds by category —— 每个方向用不同的 Google News 方向关键词搜索（精准、不重复）
# 条目 link 为 Google News 重定向地址，运行期通过 _resolve_real_url 解析为真实原文 URL
def _gnq(q):
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "Google·" + q)

FEEDS = {
    "finance": [_gnq("财经 A股 股市"), _gnq("美联储 降息"), _gnq("港股 美股 行情")],
    "ai": [_gnq("人工智能 大模型"), _gnq("AI 智能体 agent"), _gnq("ChatGPT GPT 发布 融资")],
    "ai_apps": [_gnq("AI 应用 工具"), _gnq("AI Agent 创业 产品"), _gnq("AI 编程 办公 发布")],
    "newenergy": [_gnq("新能源 光伏 储能"), _gnq("电动车 电池 比亚迪"), _gnq("碳中和 风电 氢能源")],
    "entertainment": [_gnq("电影 票房 上映"), _gnq("综艺 热播 明星 官宣"), _gnq("游戏 电竞 赛事")],
    "semiconductor": [_gnq("半导体 芯片"), _gnq("晶圆 光刻机"), _gnq("GPU 英伟达 存储 中芯国际")],
}

def _decode_google_article(glink):
    """Google News RSS 文章链接是 base64 编码的，直接解码出真实原文 URL（无需联网）。
    支持 Google News 所有已知编码前缀（HN_News/HN_World/HN_Video 等），
    并从解码内容中智能提取任何 http(s) URL。"""
    try:
        if "news.google.com" not in glink:
            return ""
        m = re.search(r'/(?:rss/)?articles/([A-Za-z0-9_-]+)', glink)
        if not m:
            return ""
        b64 = m.group(1)
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)
        decoded = raw.decode("utf-8", "ignore")

        # 策略A：已知 Google News 前缀 → 取签名后的 URL
        for prefix in ("HN_News ", "HN_World ", "HN_Video ", "HN_Local ",
                        "HN_Podcasts ", "HN_Opinion "):
            if decoded.startswith(prefix):
                url = decoded[len(prefix):].strip()
                if url.startswith("http"):
                    return url

        # 策略B：解码结果本身就是完整 URL（无前缀）
        if decoded.startswith("http"):
            return decoded.strip()

        # 策略C：从解码文本中正则提取第一个 http(s) URL（最宽松兜底）
        um = re.search(r'https?://[^\s\x00-\x1f\x7f-\x9f"\'<>]+', decoded)
        if um:
            url = um.group(0).rstrip(".,;)")
            # 排除 google.com 自身链接
            if "google.com" not in url and "google.co" not in url:
                return url

    except Exception:
        pass
    return ""


def _resolve_real_url(glink):
    """解析 Google News 重定向为真实原文 URL。
    优先 base64 解码（无需联网），失败再跟随 HTTP 重定向（CI 在美国，一定通），
    最终失败返回空（→ 百度搜索兜底）。"""
    if not glink or "news.google.com" not in glink:
        return glink  # 已是直连源真实 URL

    # 方法1：base64 解码（不依赖网络）
    decoded = _decode_google_article(glink)
    if decoded and "google.com" not in decoded and len(decoded) > 10:
        return decoded

    # 方法2：HTTP 跟随重定向（CI 在美国环境，Google 可达）
    for method_name, getter in [
        ("GET+redirect", lambda u: requests.get(u, headers=UA, timeout=12, allow_redirects=True)),
        ("HEAD", lambda u: requests.head(u, headers=UA, timeout=8, allow_redirects=True)),
    ]:
        try:
            r = getter(glink)
            # 检查最终 URL
            if r.url and "google.com" not in r.url and r.url.startswith("http"):
                return r.url
            # 检查 Location 头
            loc = (r.headers.get("Location") or r.headers.get("location") or "")
            if loc and "google.com" not in loc:
                if loc.startswith("http"):
                    return loc
                if loc.startswith("//"):
                    return "https:" + loc
            # 从响应体中提取 JS 重定向目标
            txt = r.text[:30000]
            for pattern in [
                r'url\s*=\s*["\']?(https?://[^"\'>\s]+)',
                r'["\'](https?://[^"\']*?article[^"\']*?)["\']',
                r'"(https?://[^"]+)"',
            ]:
                m = re.search(pattern, txt, re.I)
                if m and "google.com" not in m.group(1):
                    return m.group(1)
        except Exception:
            continue

    return ""

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
    "newenergy": {
        "政策与市场": ["政策", "补贴", "规划", "目标", "碳中和", "碳达峰", "双碳", "国补", "地补", "发改委"],
        "产业链动态": ["电池", "光伏", "风电", "储能", "氢能", "充电桩", "换电", "锂", "硅料", "组件", "逆变器"],
        "企业动态": ["比亚迪", "特斯拉", "宁德时代", "蔚来", "理想", "小鹏", "隆基", "通威", "阳光电源", "亿纬锂能"],
        "投融资与出海": ["融资", "投资", "上市", "出海", "出口", "海外", "欧洲", "东南亚", "估值", "IPO"],
    },
    "entertainment": {
        "影视动态": ["电影", "电视剧", "网剧", "票房", "上映", "开播", "网飞", "好莱坞", "国产"],
        "音乐与综艺": ["音乐", "综艺", "演唱会", "新歌", "专辑", "选秀", "节目", "播出"],
        "明星与热点": ["明星", "演员", "导演", "官宣", "恋情", "离婚", "结婚", "热搜"],
        "游戏与电竞": ["游戏", "电竞", "手游", "端游", "主机", "赛事", "冠军", "战队", "电竞"],
    },
    "semiconductor": {
        "政策与产业": ["政策", "补贴", "大基金", "国产替代", "半导体", "芯片", "集成电路", "产业", "规划"],
        "技术与产品": ["制程", "工艺", "光刻", "EDA", "封装", "测试", "晶圆", "代工", "GPU", "CPU", "存储"],
        "企业动态": ["台积电", "三星", "英特尔", "英伟达", "中芯国际", "华为", "海思", "长鑫", "长江存储"],
        "投融资与IPO": ["融资", "投资", "IPO", "上市", "估值", "收购", "并购", "亿元"],
    },
}

# Brief colors by type
BRIEF_CONFIG = {
    "finance": {"title": "📊 财经简报", "subtitle": "科技金融 · A股港股 · 美联储 · 一级市场", "color": "#1a1a2e", "accent": "#e74c3c", "gradient": "#1a1a2e, #16213e"},
    "ai": {"title": "🤖 AI 产业简报", "subtitle": "大模型 · 智能体 · 具身智能 · 投融资", "color": "#6c5ce7", "accent": "#6c5ce7", "gradient": "#6c5ce7, #4a3cd4"},
    "ai_apps": {"title": "🚀 AI 应用简报", "subtitle": "AI 工具 · 行业落地 · Agent · 商业模式", "color": "#27ae60", "accent": "#27ae60", "gradient": "#27ae60, #1e8449"},
    "newenergy": {"title": "⚡ 新能源简报", "subtitle": "光伏风电 · 储能电池 · 电动车 · 碳中和", "color": "#e67e22", "accent": "#e67e22", "gradient": "#e67e22, #d35400"},
    "entertainment": {"title": "🎬 娱乐简报", "subtitle": "影视综艺 · 音乐明星 · 游戏电竞 · 热点", "color": "#e91e63", "accent": "#e91e63", "gradient": "#e91e63, #c2185b"},
    "semiconductor": {"title": "💻 半导体简报", "subtitle": "芯片技术 · 晶圆代工 · EDA · 国产替代", "color": "#2196f3", "accent": "#2196f3", "gradient": "#2196f3, #1565c0"},
}

# ── Helper Functions ───────────────────────────────────

def load_cache(brief_type):
    """按方向读取缓存，避免跨方向串数据（曾导致所有方向显示同一方向内容）。"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < CACHE_HOURS * 3600:
                items = data.get("items", {})
                if isinstance(items, dict):
                    return items.get(brief_type, [])
        except: pass
    return []

def save_cache(items, brief_type):
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f).get("items", {})
        except: cache = {}
    cache[brief_type] = items
    with open(CACHE_FILE, "w") as f:
        json.dump({"ts": time.time(), "items": cache}, f)

def fetch_google_news(query, max_items=15, retries=3):
    """从 Google News 中文 RSS 抓取（云端环境稳定可达），带重试。"""
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) \
          + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            fp = feedparser.parse(io.BytesIO(r.content))
            out = []
            for e in fp.entries[:max_items]:
                title = e.get("title", "").strip()
                src = ""
                if " - " in title:
                    title, src = title.rsplit(" - ", 1)
                summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))
                summary = re.sub(r"\s+", " ", summary).strip()
                if len(summary) > 200:
                    summary = summary[:200] + "..."
                out.append({
                    "title": title,
                    "summary": summary,
                    "link": e.get("link", ""),
                    "source": src or "Google News",
                    "date": e.get("published", ""),
                })
            if out:
                return out
            print(f"  ⚠ Google News [{query}] 第{attempt+1}次返回空，重试")
        except Exception as ex:
            print(f"  ⚠ Google News [{query}] 第{attempt+1}次失败: {ex}")
        time.sleep(3)
    return []


def fetch_news(brief_type):
    """抓取新闻：每个方向用 Google News 方向关键词搜索，跟随重定向解析真实原文链接。"""
    cached = load_cache(brief_type)
    if cached:
        return cached

    items = []
    seen = set()

    # ── 主源：国内 RSS（链接可直接访问）──
    for url, source_name in FEEDS.get(brief_type, []):
        try:
            r = requests.get(url, headers=UA, timeout=10)
            fp = feedparser.parse(io.BytesIO(r.content))
            for entry in fp.entries[:15]:
                title = entry.get("title", "").strip()
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0].strip()
                if len(title) < 4 or title in seen:
                    continue
                seen.add(title)
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "") or entry.get("description", ""))
                summary = re.sub(r"\s+", " ", summary).strip()
                if len(summary) > 200:
                    summary = summary[:200] + "..."
                # 链接提取：多级兜底 + 全属性扫描 + description内<a>提取
                link = (entry.get("link", "")
                    or (next((l.href for l in getattr(entry,'links',[]) if hasattr(l,'href') and l.href), ""))
                    or entry.get("id", ""))
                # 清理 rsshub 可能附加的追踪参数
                link = re.sub(r'[?&]utm_[^&]*', '', link)
                # 兜底1：扫描所有字符串属性找 http(s) URL
                if not link or not link.startswith("http"):
                    for k, v in entry.items():
                        if isinstance(v, str) and v.startswith(("http://", "https://")) and len(v) > 15:
                            link = re.sub(r'[?&]utm_[^&]*', '', v)
                            break
                    # 兜底2：guid 的 value 子属性
                    if not link or not link.startswith("http"):
                        g = entry.get('guid')
                        if hasattr(g, 'value') and g.value.startswith(('http://', 'https://')):
                            link = g.value
                    # 兜底3：从 description/summary HTML 中提取第一个 <a href>
                    if not link or not link.startswith("http"):
                        desc_html = (entry.get("summary", "") or entry.get("description", ""))
                        a_match = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', desc_html)
                        if a_match:
                            link = a_match.group(1)
                esrc = entry.get("source")
                real_source = esrc.get("title", "") if isinstance(esrc, dict) else ""
                items.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": real_source or source_name,
                    "date": entry.get("published", "") or entry.get("updated", ""),
                })
        except Exception as e:
            print(f"  ⚠ {source_name}({url}): {e}")

    # ── 解析 Google News 重定向，拿到真实原文 URL（国内可访问）──
    google_links = [it for it in items if "news.google.com" in it.get("link", "")]
    if google_links:
        print(f"  ↳ 解析 {len(google_links)} 条 Google News 重定向为真实原文链接...")
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            resolved = list(ex.map(_resolve_real_url, [it["link"] for it in google_links]))
        for it, real in zip(google_links, resolved):
            it["link"] = real  # 解析失败则置空 → 后续百度搜索兜底

    if not items:
        print("  ⚠ 全部源抓取为空，将由 main 写入占位页避免 404")
        return []

    save_cache(items, brief_type)
    return items


def verify_items(brief_type, items):
    """自检闸门：构建前校验，避免发布坏页面（根断"反复坏"循环）。
    拦截两类顽疾：
      1) 链接大量解析失败 → 页面清一色跳百度搜索（用户反复投诉的核心痛点）
      2) 抓取为空（源全挂 / 方向错配导致无内容）
    返回 (ok: bool, msg: str)。CI 中 ok=False 时 main() 会 sys.exit(1)，
    使整个 workflow 在 Commit&Push 前中断，坏页面上不了线。"""
    if not items:
        return False, f"❌ 自检失败：{brief_type} 抓取为空（源全部不可达或方向无内容），拒绝发布空页"
    bad = empty = 0
    sample_bad_titles = []
    for it in items:
        l = it.get("link", "")
        if not l:
            empty += 1
            sample_bad_titles.append(it.get("title", "?")[:30])
        elif "news.google.com" in l:
            bad += 1
            sample_bad_titles.append(it.get("title", "?")[:30])
    total_bad = bad + empty
    ratio = total_bad / len(items)
    # 阈值：超过一半链接解析失败则拦截
    if ratio > 0.5:
        detail = f"(未解析={empty}, 仍Google重定向={bad}/{len(items)})"
        samples = "; ".join(sample_bad_titles[:5])
        return False, f"❌ {brief_type} 自检不通过: {detail}。样例: [{samples}]"
    ok_n = len(items) - total_bad
    return True, f"✅ 自检通过：{brief_type} 链接解析率 {100*ok_n/len(items):.0f}% ({ok_n}/{len(items)}条)"


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
    # 解析 accent 颜色为 "r,g,b" 供 rgba() 使用
    accent_rgb = ",".join(str(int(config["accent"][i:i+2], 16)) for i in (1, 3, 5))
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
        "政策与市场": ("📋", "policy"),
        "产业链动态": ("🔗", "market"),
        "企业动态": ("🏢", "case"),
        "投融资与出海": ("🌍", "pe"),
        "影视动态": ("🎥", "tool"),
        "音乐与综艺": ("🎵", "app"),
        "明星与热点": ("⭐", "biz"),
        "游戏与电竞": ("🎮", "agent"),
        "政策与产业": ("📋", "policy"),
        "技术与产品": ("🔬", "model"),
        "投融资与IPO": ("💰", "capital"),
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
             # 链接处理：有原链接用原链接；无链接生成百度搜索（国内可访问）
            link = item.get("link", "")
            if not link:
                link = f"https://www.baidu.com/s?wd={urllib.parse.quote(item['title'])}"
                link_source = "搜索"
            else:
                link_source = "原文"
            link_html = f'<a class="news-link" href="{escape(link)}">{link_source}</a>'
            section_items += f"""
    <div class="news-item">
      <div class="news-index {idx_cls}">{idx}</div>
      <div class="news-body">
        <div class="news-title">{escape(item['title'])}</div>
        <div class="news-summary">{escape(summary)}</div>
        <div class="news-meta">
          <span class="news-source">{escape(item['source'])}</span>
          {link_html}
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
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
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
.news-source{{font-size:11px;color:{config['accent']};background:rgba({accent_rgb},0.1);border-radius:4px;padding:2px 7px}}
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
""" + REDIRECT_SCRIPT + """
</body>
</html>"""


# ── Main ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, choices=["finance", "ai", "ai_apps", "newenergy", "entertainment", "semiconductor"])
    args = parser.parse_args()

    bt = args.type
    file_map = {"finance": "finance.html", "ai": "ai.html", "ai_apps": "ai-apps.html", "newenergy": "newenergy.html", "entertainment": "entertainment.html", "semiconductor": "semiconductor.html"}
    output_file = file_map[bt]
    config = BRIEF_CONFIG[bt]

    print(f"🔍 正在生成 {config['title']}...")

    # Fetch
    items = fetch_news(bt)
    if not items:
        print(f"  ⚠ {config['title']} 抓取为空，写入占位页避免 404")
        categorized = {c: [] for c in CATEGORIES.get(bt, {})}
        html = build_html(bt, categorized, None)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  ✅ 已生成占位页 {output_file}")
        return

    # ── 自检闸门：坏数据绝不发布（根断"反复坏"循环）──
    ok, vmsg = verify_items(bt, items)
    print(vmsg)
    if not ok:
        print("  ⛔ 构建被自检拦截")
        # 写诊断文件（含原始链接样本），CI 会提交到仓库供排查
        diag_file = f"debug-{bt}.json"
        samples = []
        for it in items[:10]:  # 最多取 10 条样本
            l = it.get("link", "")
            is_bad = (not l) or ("news.google.com" in l)
            # 对 Google News 链接尝试解码并记录中间结果
            decode_detail = ""
            if "news.google.com" in l and "/articles/" in l:
                import base64 as _b64, re as _re
                _m = _re.search(r'/articles/([A-Za-z0-9_-]+)', l)
                if _m:
                    _b = _m.group(1); _b += "=" * (-len(_b) % 4)
                    try:
                        _raw = _b64.urlsafe_b64decode(_b)
                        _dec = _raw.decode("utf-8", "ignore")
                        decode_detail = f"| b64_len={len(_raw)} | decoded_head={_dec[:120]}"
                    except Exception as _ex:
                        decode_detail = f"| b64_decode_err={_ex}"
            samples.append({
                "title": it.get("title", "")[:60],
                "raw_link": l[:150],
                "is_bad": is_bad,
                "decode_detail": decode_detail,
            })
        # 额外：抓取第一条 Google News 原始 entry 全量字段（只做一次，全局文件）
        if not os.path.exists("debug-raw-entry.json"):
            try:
                _test_url = list(FEEDS.get(bt, []))[0][0] if FEEDS.get(bt) else ""
                if _test_url:
                    _tr = requests.get(_test_url, headers=UA, timeout=15)
                    _tfp = feedparser.parse(io.BytesIO(_tr.content))
                    if _tfp.entries:
                        _e0 = _tfp.entries[0]
                        _raw_entry = {}
                        for _k in dir(_e0):
                            if not _k.startswith("_"):
                                _v = getattr(_e0, _k, None)
                                if _v is not None and not callable(_v):
                                    try:
                                        _raw_entry[_k] = str(_v)[:500]
                                    except: pass
                        if hasattr(_e0, 'links'):
                            _raw_entry["_links_array"] = [
                                {"href": getattr(l,'href',''), "rel": getattr(l,'rel',''), "type": getattr(l,'type','')}
                                for l in (_e0.links or [])
                            ]
                        with open("debug-raw-entry.json", "w", encoding="utf-8") as _rf:
                            json.dump({"feed_url": _test_url, "entry_fields": _raw_entry}, _rf, ensure_ascii=False, indent=2)
            except Exception as _diag_ex:
                with open("debug-raw-entry.json", "w") as _rf:
                    json.dump({"error": str(_diag_ex)}, _rf)
        with open(diag_file, "w", encoding="utf-8") as _df:
            json.dump({"type": bt, "total": len(items), "msg": vmsg, "samples": samples}, _df, ensure_ascii=False, indent=2)
        print(f"  📋 诊断数据已写入 {diag_file}（将随 CI 提交）")
        sys.exit(1)

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
