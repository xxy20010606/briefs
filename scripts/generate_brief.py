#!/usr/bin/env python3
"""Daily Brief Generator - 日报生成器
Usage: python generate_brief.py --type [finance|ai|ai_apps|newenergy|entertainment|semiconductor]
"""
import argparse, json, os, re, sys, time, io, base64, concurrent.futures as cf
import urllib.parse
import xml.etree.ElementTree as ET
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

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
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

def _is_google_url(url):
    """判断 URL 是否属于 Google 系域名。
    关键：Google News 文章页里大量资源（站点图标/JS/图片）挂在
    lh3.googleusercontent.com、gstatic.com 等域名，它们【不含 'google.com' 子串】，
    旧逻辑用 `if "google.com" not in u` 判断会漏网，把 Google 图标当成'真实出版方'。
    这里用真实域名后缀严格判断。"""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return True
    if not host:
        return True
    return (host == "google.com" or host.endswith(".google.com")
            or "googleusercontent.com" in host
            or host.endswith(".gstatic.com") or host.endswith(".googleapis.com")
            or host.endswith(".googlevideo.com")
            or "google-analytics" in host
            or "googlesyndication" in host or "doubleclick" in host
            or "googleadservices" in host)


def _decode_google_article(glink):
    """Google News RSS 文章链接是 base64 编码的 protobuf。
    【关键原理】实际的出版方原文 URL 以明文 ASCII 形式嵌在 protobuf 字节流中，
    直接用正则从解码字节抠出来即可——纯本地、无需网络、且能拿到新浪/cls 等
    国内可达的出版方域名。这是比 HTTP 重定向更可靠的主力路径。
    返回真实出版方 URL 或空字符串（调用方继续尝试 HTTP 或回退）。"""
    try:
        if "news.google.com" not in glink:
            return ""
        m = re.search(r'/(?:rss/)?articles/([A-Za-z0-9_-]+)', glink)
        if not m:
            return ""
        b64 = m.group(1)
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)

        # 从 protobuf 字节流里抠所有可见的 http(s) URL（出版方 URL 是明文 ASCII）
        for fm in re.finditer(rb'https?://[^\x00-\x1f\x7f-\x9f"\'<>\s]+', raw):
            u = fm.group(0).decode("utf-8", "ignore").rstrip('.,;)')
            if u.startswith("http") and not _is_google_url(u):
                print(f"    [PROTOBUF] ✅ 抠出出版方URL: {u[:120]}")
                return u

        # 兼容旧格式：文本前缀 HN_News <URL> 等（2026年前）
        try:
            decoded = raw.decode("utf-8", "ignore")
            for prefix in ("HN_News ", "HN_World ", "HN_Video ", "HN_Local ", "HN_Opinion "):
                if decoded.startswith(prefix):
                    url = decoded[len(prefix):].strip()
                    if url.startswith("http") and not _is_google_url(url):
                        return url
        except Exception:
            pass
    except Exception as ex:
        print(f"    [PROTOBUF] ❌ 异常: {ex}")
    return ""


def _resolve_via_batchexecute(glink):
    """用 Google 官方 batchexecute API 解码 article token → 真实出版方 URL。

    【为什么需要它】Google News 的 /articles/<token> 页面现在靠 JS 动态跳转，
    纯 requests 拿不到最终 URL（只能拿到中间页/consent页）。而 Google 内部
    DotsSplashUi batchexecute 接口能直接返回该 token 对应的真实原文地址，
    纯 HTTP、无需浏览器、CI(美国)可达 —— 是目前最可靠的解析路径。

    返回真实出版方 URL 或空字符串。
    """
    try:
        m = re.search(r'/articles/([A-Za-z0-9_\-]+)', glink)
        if not m:
            return ""
        token = m.group(1)

        # Step 1: GET 文章页，提取签名(signature)与时间戳(timestamp)
        r = requests.get(f"https://news.google.com/rss/articles/{token}",
                         headers=UA, timeout=15)
        html = r.text or ""
        sg = re.search(r'data-n-a-sg=["\']([^"\']+)["\']', html)
        ts = re.search(r'data-n-a-ts=["\']([^"\']+)["\']', html)
        if not (sg and ts):
            print(f"    [BATCH] ❌ 未取到签名 (status={r.status_code}, len={len(html)})")
            return ""
        signature, timestamp = sg.group(1), ts.group(1)

        # Step 2: POST batchexecute 请求真实 URL
        inner = ["garturlreq",
                 [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                   None, None, None, None, None, 0, 1],
                  "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                 token, timestamp, signature]
        freq = json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
        body = "f.req=" + urllib.parse.quote(freq)

        r2 = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={**UA,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data=body, timeout=15)

        # Step 3: 从返回里解析 garturlres → 真实 URL
        text = r2.text or ""
        if "garturlres" not in text:
            print(f"    [BATCH] ❌ 返回中无 garturlres (status={r2.status_code}, len={len(text)})")
            return ""
        for line in text.split("\n"):
            if "garturlres" not in line:
                continue
            try:
                arr = json.loads(line)
                # arr 形如 [["wrb.fr","Fbv4je",'<json字符串>',...]]
                for part in arr:
                    if isinstance(part, list) and len(part) >= 3 and isinstance(part[2], str):
                        payload = json.loads(part[2])
                        # payload 形如 ["garturlres","<真实URL>"]
                        if isinstance(payload, list) and len(payload) >= 2:
                            url = payload[1]
                            if isinstance(url, str) and url.startswith("http") \
                                    and not _is_google_url(url):
                                print(f"    [BATCH] ✅ 解码出版方URL: {url[:120]}")
                                return url
            except Exception:
                continue
        print(f"    [BATCH] ❌ 解析返回失败")
    except Exception as ex:
        print(f"    [BATCH] ❌ 异常: {ex}")
    return ""


def _resolve_real_url(glink, source_url=None):
    """解析 Google News 链接为真实原文 URL（国内可达）。

    优先级（从高到低）：
      0) source_url：来自 RSS 抓取阶段，无需二次网络，是真实出版方域名（国内可达）。
         这是最可靠的路径——batchexecute/HTTP 在 CI 网络受限时均不稳定，而
         source_url 直接从原始 RSS 拿，绕开所有二次请求。
      1) batchexecute 官方 API 解码（若 CI 网络可达，可能更精确到具体文章页）
      2) protobuf 字节抠明文出版方 URL（旧格式有效）
      3) HTTP 跟随重定向 + HTML 结构化提取
      极端兜底：返回原始 glink（news.google.com 链接）
    """
    if not glink or "news.google.com" not in glink:
        return glink  # 已是直连源真实 URL

    # 优先级0：source url（出版方真实域名，国内可达，无需二次网络）
    if source_url and source_url.startswith("http") and not _is_google_url(source_url):
        print(f"    [SOURCE] ✅ 使用出版方 source url: {source_url[:120]}")
        return source_url

    # 方法1：batchexecute 官方 API（最优路径）
    real = _resolve_via_batchexecute(glink)
    if real and not _is_google_url(real) and len(real) > 10:
        return real

    # 方法2：从 protobuf 明文抠出版方 URL
    decoded = _decode_google_article(glink)
    if decoded and not _is_google_url(decoded) and len(decoded) > 10:
        return decoded

    # 方法2：HTTP 跟随重定向（CI 在美国环境，Google 可达）
    try:
        # 关键：去掉 /rss/ 前缀。RSS 路径会让 Google 直接返回 XML（不会重定向），
        # 必须走 /articles/ 路径才会触发 Google 的标准 302 → 出版方跳转。
        http_url = re.sub(r'(news\.google\.com)/rss/articles/', r'\1/articles/', glink)
        if http_url != glink:
            print(f"    [HTTP] 转换路径: {glink[:80]} → {http_url[:80]}")
        r = requests.get(http_url, headers=UA, timeout=18, allow_redirects=True)
        print(f"    [HTTP] GET status={r.status_code} final_url={r.url[:120]}")

        # 2a. 最终URL已是出版方（非 Google 系）
        if r.url and not _is_google_url(r.url) and r.url.startswith("http"):
            print(f"    [HTTP] ✅ 最终URL即出版方: {r.url[:120]}")
            return r.url

        # 2b. 从最终 HTML 解析真实 URL（多种模式，严格过滤 Google 系）
        txt = r.text or ""
        candidates = []
        # 优先级 1: Google News 文章页官方标识 publisher URL 的 data-source-url 属性
        # 这是最可靠的解析点（直接由 Google 提供）
        for pat in [
            r'data-source-url=["\']([^"\']+)["\']',
            r'data-source-url=&quot;([^&]+)&quot;',  # HTML 实体编码版
        ]:
            for m in re.finditer(pat, txt, re.I):
                u = m.group(1).replace("&amp;", "&")
                if u.startswith("http") and not _is_google_url(u):
                    candidates.append(u)

        # 优先级 2: 结构化元数据 (og:url / canonical)
        for pat in [
            r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
            r'<base[^>]+href=["\']([^"\']+)["\']',
        ]:
            m = re.search(pat, txt, re.I)
            if m:
                candidates.append(m.group(1))

        # 优先级 3: 阅读/查看按钮 (article-link / Read / View original)
        for pat in [
            r'<a[^>]+class=["\'][^"\']*article-link[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*<span[^>]*>\s*(?:Read full article|View original|查看原文|阅读全文)',
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:Read full article|View original|查看原文|阅读全文)',
        ]:
            m = re.search(pat, txt, re.I)
            if m:
                # article-link 模式 capture groups 可能有 class/href，取最后一个（href）
                cand = m.groups()[-1]
                if cand:
                    candidates.append(cand)

        # 优先级 4: 限定安全的兜底——从 HTML 中找一个**长度合理**且**非 Google 系**的 https URL
        # 仅取第一段内容区域（避免抓到 footer/analytics 区域）
        # 截取前 30KB（内容区域通常在前面）以减少误匹配
        body = txt[:30000] if len(txt) > 30000 else txt
        for m in re.finditer(r'https?://[^\s"\'<>]+', body):
            u = m.group(0).rstrip('.,;)')
            if (not _is_google_url(u)
                    and len(u) > 25  # 真实出版方URL通常较长
                    and '?' in u or '/' in u[8:]  # 有路径或参数
                    and not u.endswith('.js')  # 排除 JS 文件
                    and not u.endswith('.css')
                    and not u.endswith('.png')
                    and not u.endswith('.jpg')
                    and not u.endswith('.ico')
                    and not u.endswith('.json')
                    and not u.endswith('.xml')):
                candidates.append(u)
                break  # 只取第一个合格的

        for c in candidates:
            if c.startswith("http") and not _is_google_url(c) and len(c) > 15:
                print(f"    [HTTP] ✅ 从HTML解析出版方: {c[:120]}")
                return c

        # 诊断：把 HTTP 失败的样本落盘（含状态码/最终URL/HTML片段），便于定位
        _diag(f"HTTP_FAIL status={r.status_code} final={r.url[:150]} "
              f"len={len(txt)} candidates={len(candidates)}")
        try:
            with io.open("debug-http-fail.html", "w", encoding="utf-8") as fh:
                fh.write(f"REQ_URL: {http_url}\nSTATUS: {r.status_code}\n"
                         f"FINAL_URL: {r.url}\nHTML_LEN: {len(txt)}\n\n"
                         f"---HTML(前4000字符)---\n{txt[:4000]}")
        except Exception:
            pass
    except Exception as ex:
        print(f"    [HTTP] ❌ 异常: {ex}")
        _diag(f"EXCEPTION: {ex!r}")

    print(f"    [HTTP] ⚠ 解析失败，回退保留 Google News 原始链接（浏览器可跳转）")
    _diag(f"FAIL -> fallback to glink: {glink[:100]}")
    return glink  # 极端兜底：保留原始链接，不用空(→百度)、不用Google图片

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

# 从原始 XML 文本中抠出 Google News 文章 URL（结构无关，兼容 <link>文本 / <link href> / 命名空间前缀）
_GN_URL_RE = re.compile(r'https://news\.google\.com/rss/articles/[A-Za-z0-9_-]+(?:\?[^"<\s]*)?')

def _extract_items_from_xml(content):
    """从 Google News RSS 原始字节/文本提取条目。

    【设计铁律】Google News 的 RSS 结构不稳定（<link> 可能带命名空间、自闭合、
    甚至直接是站点图标 URL），ElementTree.findtext 在命名空间下会静默返回空。
    故改用「纯正则」从原始文本切片提取，彻底绕开命名空间问题。

    链接提取【只接受】news.google.com/rss/articles/ 文章 URL：
      - 优先 <link> 文本 / <link href> 自闭合
      - 其次 <description> 内转义的 <a href="news.google.com/rss/articles/...">
      - 绝不接受 lh3.googleusercontent.com(图标)/google-analytics/gstatic 等资源 URL
    若某条目无论如何都抠不到文章 URL，link 留空（→ 后续百度搜索兜底，也比图标好）。
    """
    if isinstance(content, bytes):
        text = content.decode("utf-8", "ignore")
    else:
        text = content

    blocks = re.findall(r'<item\b[^>]*>.*?</item>', text, re.S)
    if not blocks:
        blocks = re.findall(r'<entry\b[^>]*>.*?</entry>', text, re.S)

    ARTICLE_RE = re.compile(r'https://news\.google\.com/rss/articles/[A-Za-z0-9_.\-]+(?:\?[^\s"\'<>&]*)?')
    BAD_RE = re.compile(r'googleusercontent\.com|google-analytics\.com|gstatic\.com|googleapis\.com')

    items = []
    for blk in blocks:
        b = (blk.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'"))

        tm = re.search(r'<title[^>]*>(.*?)</title>', b, re.S)
        title = re.sub(r'<[^>]+>', '', tm.group(1)).strip() if tm else ""
        title = re.sub(r'\s+', ' ', title)

        link = ""
        lm = re.search(r'<link[^>]*>(.*?)</link>', b, re.S)
        if lm and ARTICLE_RE.search(lm.group(1)):
            link = ARTICLE_RE.search(lm.group(1)).group(0)
        if not link:
            lm = re.search(r'<link[^>]*href=["\'](https://news\.google\.com/rss/articles/[^"\']+)["\']', b)
            if lm:
                link = lm.group(1)
        if not link:
            lm = re.search(r'<a[^>]*href=["\'](https://news\.google\.com/rss/articles/[^"\']+)["\']', b)
            if lm:
                link = lm.group(1)
        if not link:
            lm = ARTICLE_RE.search(b)
            if lm:
                link = lm.group(0)
        if link and BAD_RE.search(link):
            link = ""

        dm = re.search(r'<description[^>]*>(.*?)</description>', b, re.S)
        if not dm:
            dm = re.search(r'<summary[^>]*>(.*?)</summary>', b, re.S)
        desc = re.sub(r'<[^>]+>', '', dm.group(1)).strip() if dm else ""
        desc = re.sub(r'\s+', ' ', desc)

        pm = re.search(r'<pubDate[^>]*>(.*?)</pubDate>', b, re.S)
        if not pm:
            pm = re.search(r'<updated[^>]*>(.*?)</updated>', b, re.S)
        date = pm.group(1).strip() if pm else ""

        sm = re.search(r'<source[^>]*>([^<]+)</source>', b)
        source = sm.group(1).strip() if sm else ""
        # 出版方真实域名 URL（来自 RSS 原始数据，无需二次网络请求，国内可达）
        sm2 = re.search(r'<source[^>]*url=["\']([^"\']+)["\']', b)
        source_url = sm2.group(1).strip() if sm2 else ""
        if not source and source_url:
            # 个别 feed 只有 url 没有文本，用 host 作为来源名
            try:
                source = urllib.parse.urlparse(source_url).netloc.lower()
            except Exception:
                pass

        if " - " in title:
            t2, src2 = title.rsplit(" - ", 1)
            title, src2 = t2.strip(), src2.strip()
            if src2 and not source:
                source = src2

        if title:
            items.append({
                "title": title,
                "link": link,
                "source_url": source_url,  # 出版方真实域名（国内可达，解析主力）
                "description": desc,
                "date": date,
                "source": source,
            })
    return items

def _fetch_one_feed(url, source_name, seen):
    """抓取单个 RSS 源，返回条目列表（已去重、已清洗标题/摘要）。"""
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=15)
        raw_items = _extract_items_from_xml(r.content)
        print(f"  ↳ {source_name}: 解析到 {len(raw_items)} 条，"
              f"其中带 Google 链接 {sum(1 for x in raw_items if 'news.google.com' in (x.get('link') or ''))} 条")
        # 兜底：ElementTree 一条都没解析出来时，用 feedparser 再试一次
        if not raw_items:
            fp = feedparser.parse(io.BytesIO(r.content))
            for e in fp.entries:
                out.append({
                    "title": e.get("title", "").strip(),
                    "link": (e.get("link", "") or
                             next((l.href for l in getattr(e, "links", []) if getattr(l, "href", "")), "")),
                    "description": e.get("summary", "") or e.get("description", ""),
                    "date": e.get("published", "") or e.get("updated", ""),
                    "source": (e.get("source", {}).get("title", "") if isinstance(e.get("source"), dict) else ""),
                    # feedparser 兜底分支也要带出版方真实域名，否则回退 news.google.com
                    "source_url": (e.get("source", {}).get("href", "") if isinstance(e.get("source"), dict) else ""),
                })
            return out
        for ri in raw_items[:15]:
            title = ri["title"]
            src = ""
            if " - " in title:
                title, src = title.rsplit(" - ", 1)
                title, src = title.strip(), src.strip()
            if len(title) < 4 or title in seen:
                continue
            seen.add(title)
            summary = re.sub(r"<[^>]+>", "", ri["description"] or "")
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 200:
                summary = summary[:200] + "..."
            link = re.sub(r'[?&]utm_[^&]*', '', (ri["link"] or "").strip())
            # 兜底：从 description HTML 内 <a href> 提取
            if not link.startswith("http"):
                a = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', ri["description"] or "")
                if a:
                    link = a.group(1)
            out.append({
                "title": title,
                "summary": summary,
                "link": link,
                "source_url": ri.get("source_url", "") or "",  # 出版方真实域名(国内可达)，解析主力
                "source": src or ri["source"] or source_name,
                "date": ri["date"],
            })
    except Exception as e:
        print(f"  ⚠ {source_name}({url}): {e}")
    return out


def fetch_news(brief_type):
    """抓取新闻：每个方向用 Google News 方向关键词搜索，跟随重定向解析真实原文链接。"""
    cached = load_cache(brief_type)
    if cached:
        return cached

    items = []
    seen = set()
    for url, source_name in FEEDS.get(brief_type, []):
        items.extend(_fetch_one_feed(url, source_name, seen))

    # ── 诊断落盘（始终执行）：CI 后直接读 debug-fetch-{bt}.json 看清
    #    每条 title/link/source_url 提取结果，定位"链接为空/回退百度"顽疾 ──
    try:
        _diag_samples = [{
            "title": (it.get("title", "") or "")[:50],
            "link": (it.get("link", "") or "")[:140],
            "source_url": (it.get("source_url", "") or "")[:140],
            "source": (it.get("source", "") or ""),
        } for it in items[:6]]
        with io.open(f"debug-fetch-{brief_type}.json", "w", encoding="utf-8") as _df:
            json.dump({"type": brief_type, "total": len(items), "samples": _diag_samples},
                      _df, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # ── 解析 Google News 重定向，拿到真实原文 URL（国内可访问）──
    google_links = [it for it in items if "news.google.com" in it.get("link", "")]
    if google_links:
        print(f"  ↳ 解析 {len(google_links)} 条 Google News 重定向为真实原文链接...")
        def _resolve_one(it):
            return _resolve_real_url(it["link"], it.get("source_url"))
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            resolved = list(ex.map(_resolve_one, google_links))
        for it, real in zip(google_links, resolved):
            it["link"] = real  # 解析失败则置空 → 后续百度搜索兜底

    if not items:
        print("  ⚠ 全部源抓取为空，将由 main 写入占位页避免 404")
        return []

    save_cache(items, brief_type)
    return items


def verify_items(brief_type, items):
    """自检闸门：构建前校验，避免发布坏页面（根断"反复坏"循环）。
    仅拦截两类真正会导致坏页面的顽疾：
      1) 链接大量为空 → 页面清一色跳百度搜索（用户反复投诉的核心痛点）
      2) 抓取为空（源全挂 / 方向错配导致无内容）
    注意：news.google.com 文章链接本身可点击、浏览器中会重定向到真实原文，
    不属于"坏页面"，故不再视为拦截项（仅作统计展示）。"""
    if not items:
        return False, f"❌ 自检失败：{brief_type} 抓取为空（源全部不可达或方向无内容），拒绝发布空页"
    empty = 0
    sample_bad_titles = []
    google_n = 0
    for it in items:
        l = it.get("link", "")
        if not l:
            empty += 1
            sample_bad_titles.append(it.get("title", "?")[:30])
        elif "news.google.com" in l:
            google_n += 1
    ratio = empty / len(items)
    # 阈值：超过一半链接为空（→ 百度搜索兜底）才拦截
    if ratio > 0.5:
        detail = f"(空链接={empty}/{len(items)}, Google文章链接={google_n})"
        samples = "; ".join(sample_bad_titles[:5])
        return False, f"❌ {brief_type} 自检不通过: {detail}。样例: [{samples}]"
    ok_n = len(items) - empty
    return True, f"✅ 自检通过：{brief_type} 有效链接率 {100*ok_n/len(items):.0f}% (空{empty}/Google{google_n}/{len(items)}条)"


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
        # 额外：把第一条条目【原始 XML】dump 出来（终极诊断，看清 Google News 真实结构，每次覆盖）
        try:
            _test_url = list(FEEDS.get(bt, []))[0][0] if FEEDS.get(bt) else ""
            if _test_url:
                _tr = requests.get(_test_url, headers=UA, timeout=15)
                _root = ET.fromstring(_tr.content)
                _nodes = _root.findall(".//item") or _root.findall(".//entry")
                if _nodes:
                    _raw_xml = ET.tostring(_nodes[0], encoding="unicode")[:2000]
                else:
                    _raw_xml = "(无 item/entry 节点) 原始前500字符:\n" + (_tr.text[:500] if _tr.text else "(空响应)")
                with open("debug-raw-entry.xml", "w", encoding="utf-8") as _rf:
                    _rf.write(_raw_xml)
        except Exception as _diag_ex:
            with open("debug-raw-entry.xml", "w", encoding="utf-8") as _rf:
                _rf.write("诊断失败: " + str(_diag_ex))
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
