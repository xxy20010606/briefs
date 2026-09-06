# -*- coding: utf-8 -*-
"""漏斗 mock 测试：4 场景验证 保条目 + 防单一 + 去重 + 极端兜底"""
import importlib.util, datetime
from collections import Counter

SPEC = importlib.util.spec_from_file_location(
    "gb", r"C:\Users\XX\briefs-tmp\scripts\generate_brief.py")
gb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gb)

today = datetime.date.today()


def durl(host, days_ago, n):
    d = today - datetime.timedelta(days=days_ago)
    return f"https://{host}/news/{d.isoformat()}/a{n}.html"


def mk(host, days_ago, n):
    return {"title": f"{host}-{n}", "link": durl(host, days_ago, n),
            "source": host, "source_url": ""}


def run(name, all_items, checks):
    gb.load_cache = lambda bt: None
    gb._fetch_one_feed = lambda url, nm, seen: all_items
    gb.save_cache = lambda items, bt: None
    out = gb.fetch_news("finance")
    cnt = Counter(gb._domain_of(it["link"]) for it in out)
    print(f"[{name}] 最终 {len(out)} 条, 域名分布: {dict(cnt)}")
    for i, c in enumerate(checks):
        assert c(out, cnt), f"{name} check#{i} 失败"
    print(f"{name} PASS")


# A：24h 全是 cnfol 单一来源 → 回填其他域名，cnfol 被压到 ≤2
mockA = [mk("cnfol.com", 0, i) for i in range(5)] + [
    mk("nbd.com.cn", 2, 1), mk("cls.cn", 2, 2), mk("yicai.com", 1, 3),
    mk("eastmoney.com", 2, 4)]
run("A", mockA, [
    lambda out, cnt: len(out) == 7,
    lambda out, cnt: cnt.get("cnfol.com", 0) <= 3,
    lambda out, cnt: cnt.get("cnfol.com", 0) <= 2 or sum(1 for v in cnt.values() if v >= 1) >= 4,
    lambda out, cnt: any("nbd.com.cn" in it["link"] for it in out),
    lambda out, cnt: len(set(it["link"] for it in out)) == len(out),
])

# B：24h 内 7 条且来源多样 → 不触发回填
mockB = [mk(h, 0, i) for i, h in enumerate(
    ["cnfol.com", "cls.cn", "yicai.com", "eastmoney.com", "nbd.com.cn",
     "xueqiu.com", "gelonghui.com"])]
run("B", mockB, [
    lambda out, cnt: len(out) == 7,
    lambda out, cnt: max(cnt.values()) <= 2,
])

# C：池子总共只有 5 条（含 URL 重复×3 模拟跨查询重复）→ 去重后 5 条全出
mockC = [mk("cls.cn", 0, 1), mk("yicai.com", 0, 2), mk("cnfol.com", 0, 3),
         mk("nbd.com.cn", 1, 4), mk("xueqiu.com", 2, 5)] * 3
run("C", mockC, [
    lambda out, cnt: len(out) == 5,
    lambda out, cnt: len(set(it["link"] for it in out)) == len(out),
])

# D：全部旧文+非白名单 → 极端兜底不崩（回原始池）
mockD = [mk("example.com", 10, 1), mk("foo.bar", 10, 2)]
run("D", mockD, [lambda out, cnt: isinstance(out, list)])

# E：博彩软文标题过滤 + 车家号子域排除
assert gb._is_junk_title("雷竞技(RAYBET)官方网站-顶级电竞体育：全面解析")
assert gb._is_junk_title("劲爆体育哪个app有？2026秋季游戏季")
assert not gb._is_junk_title("暑期档收官：总票房124.98亿实现两连增")
assert not gb._is_junk_title("券商开户潮来袭")  # 财经正经词不误杀
assert not gb._is_trusted("https://chejiahao.autohome.com.cn/123.html")
assert gb._is_trusted("https://www.autohome.com.cn/123.html")
mockE = [{"title": "雷竞技(RAYBET)官方网站", "link": durl("cn.chinadaily.com.cn", 0, 1),
          "source": "", "source_url": ""}] + [
    mk("cls.cn", 0, i) for i in range(6)]
run("E", mockE, [
    lambda out, cnt: len(out) == 6,
    lambda out, cnt: all("雷竞技" not in it["title"] for it in out),
])

# F：categorize 溢出补位（7 条集中在 2 类也显示满 7 条）
itemsF = [{"title": f"电影票房测试{i}", "link": durl("m1905.com", 0, i),
           "source": "", "source_url": ""} for i in range(5)] + \
         [{"title": f"游戏电竞测试{i}", "link": durl("gamersky.com", 0, i),
           "source": "", "source_url": ""} for i in range(2)]
categorized = gb.categorize(itemsF, "entertainment")
total = sum(len(v) for v in categorized.values())
print(f"[F] categorize 后共 {total} 条: { {k: len(v) for k, v in categorized.items()} }")
assert total == 7, f"F 应7条, 实际{total}"
print("F PASS")
print("ALL_FUNNEL_TESTS_OK")
