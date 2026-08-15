#!/usr/bin/env python3
"""每天自动更新 index.html 的防缓存参数 ?v=当天 与日期占位。

在 GitHub Actions 里于生成完各方向简报后运行：
- 把所有卡片链接的 ?v=YYYYMMDD 替换为当天
- 把 header-date / 最后更新 的静态占位更新为当天
（页面内已有 JS 在运行时动态填充，这里只是保证静态快照也是最新的，
 同时保证链接带当天参数，命中客户端强缓存时也能拉到最新。）
"""
import re
import os
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")


def main():
    if not os.path.isfile(INDEX):
        print("index.html not found, skip")
        return

    with open(INDEX, "r", encoding="utf-8") as f:
        html = f.read()

    now = datetime.datetime.now()
    v = now.strftime("%Y%m%d")
    cn = now.strftime("%Y年%m月%d日")
    hm = now.strftime("%H:%M")

    # 1) 卡片链接的防缓存参数
    html = re.sub(r"\?v=\d{8}", f"?v={v}", html)
    # 2) header-date 静态占位
    html = re.sub(r'(id="today-date">)\d{4}年\d{1,2}月\d{1,2}日',
                  lambda m: m.group(1) + cn, html)
    # 3) 最后更新时间
    html = re.sub(r'最后更新：\d{4}年\d{1,2}月\d{1,2}日 \d{1,2}:\d{2}',
                  f'最后更新：{cn} {hm}', html)

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"index.html 已更新 -> v={v} {cn} {hm}")


if __name__ == "__main__":
    main()
