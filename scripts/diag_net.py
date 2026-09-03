#!/usr/bin/env python3
"""网络与 Google News URL 解析诊断（CI 专用）。

目的：CI 日志 API 403 拉不到，只能靠把诊断结果**落盘并 commit** 才能看到。
本脚本独立运行，不影响简报生成。依次测试：
  1) 能否访问 news.google.com RSS（内容抓取能力）
  2) 能否 GET news.google.com/articles/<token>（HTTP 解析能力）
  3) batchexecute 官方 API 能否解出真实出版方 URL
结果写入 debug-net-diag.txt（CI 会 git add 该文件）。
"""
import io, json, re, sys, urllib.parse

import requests

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

OUT = []


def log(msg):
    print(msg)
    OUT.append(str(msg))


def get_token_from_rss():
    """从 Google News RSS 取一个真实 article token。"""
    q = "AI 产业"
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
           + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    try:
        r = requests.get(url, headers=UA, timeout=20)
        log(f"[RSS] status={r.status_code} len={len(r.text)}")
        m = re.search(r'https://news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)', r.text)
        if m:
            log(f"[RSS] ✅ 取到 token: {m.group(1)[:40]}...")
            return m.group(1), r.text[:500]
        log("[RSS] ❌ 未找到 article token")
        return None, r.text[:500]
    except Exception as e:
        log(f"[RSS] ❌ 异常: {e!r}")
        return None, ""


def test_http_resolve(token):
    """测试 HTTP 跟随重定向能否拿到出版方 URL（/rss/ vs /articles/ 两种路径）。"""
    for label, path in (("rss路径", f"https://news.google.com/rss/articles/{token}"),
                        ("articles路径", f"https://news.google.com/articles/{token}")):
        try:
            r = requests.get(path, headers=UA, timeout=20, allow_redirects=True)
            log(f"[{label}] status={r.status_code} final={r.url[:150]}")
            log(f"[{label}] html_len={len(r.text)}")
            # 是否含 data-n-a-sg（batchexecute 所需签名）
            sg = re.search(r'data-n-a-sg=["\']([^"\']+)["\']', r.text)
            ts = re.search(r'data-n-a-ts=["\']([^"\']+)["\']', r.text)
            log(f"[{label}] data-n-a-sg={'有' if sg else '无'} data-n-a-ts={'有' if ts else '无'}")
            if sg and ts:
                return sg.group(1), ts.group(1), r.text
            # 打印 HTML 前 800 字符帮助判断页面类型
            log(f"[{label}] HTML片段: {r.text[:800]}")
        except Exception as e:
            log(f"[{label}] ❌ 异常: {e!r}")
    return None, None, ""


def test_batchexecute(token, sg, ts):
    """测试 batchexecute 官方 API 能否解出真实 URL。"""
    try:
        inner = ["garturlreq",
                 [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                   None, None, None, None, None, 0, 1],
                  "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                 token, ts, sg]
        freq = json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
        body = "f.req=" + urllib.parse.quote(freq)
        r = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={**UA,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data=body, timeout=20)
        log(f"[BATCH] status={r.status_code} len={len(r.text)}")
        if "garturlres" in r.text:
            for line in r.text.split("\n"):
                if "garturlres" not in line:
                    continue
                try:
                    arr = json.loads(line)
                    for part in arr:
                        if isinstance(part, list) and len(part) >= 3 and isinstance(part[2], str):
                            payload = json.loads(part[2])
                            if isinstance(payload, list) and len(payload) >= 2:
                                log(f"[BATCH] ✅ 真实URL: {payload[1]}")
                                return payload[1]
                except Exception as e:
                    log(f"[BATCH] 解析行失败: {e!r}")
        else:
            log(f"[BATCH] ❌ 返回无 garturlres")
            log(f"[BATCH] 返回片段: {r.text[:800]}")
    except Exception as e:
        log(f"[BATCH] ❌ 异常: {e!r}")
    return None


def main():
    log("=" * 60)
    log("Google News URL 解析诊断")
    log("=" * 60)

    token, rss_snippet = get_token_from_rss()
    if not token:
        log("\n结论：RSS 都取不到，网络层面完全不可达 Google")
        log(f"RSS片段: {rss_snippet}")
        write_out()
        return

    sg, ts, html = test_http_resolve(token)
    if sg and ts:
        url = test_batchexecute(token, sg, ts)
        if url:
            log("\n结论：✅ batchexecute 可解出真实 URL，方案可行")
        else:
            log("\n结论：❌ batchexecute 解码失败（签名取到了但 API 未返回 URL）")
    else:
        log("\n结论：❌ 拿不到签名 data-n-a-sg / data-n-a-ts，batchexecute 无法执行")
        log(f"HTML前1500字符:\n{html[:1500]}")

    write_out()


def write_out():
    try:
        with io.open("debug-net-diag.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(OUT))
        print("\n[DIAG] 已写入 debug-net-diag.txt")
    except Exception as e:
        print(f"[DIAG] 写文件失败: {e!r}")


if __name__ == "__main__":
    main()
