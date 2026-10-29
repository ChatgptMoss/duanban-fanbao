#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
断板反包 · 盘中实时判定

用法：
    python live.py auction    # 9:26 跑，判竞价（条件①）
    python live.py confirm    # 9:41 跑，判开盘承接（条件②③）

判定条件：
    ① 竞价涨幅 >= -3%
    ② 开盘后 10 分钟成交额 >= 3 亿
    ③ 主线篮子里 >= 2 只翻红

数据源：腾讯行情（qt.gtimg.cn，GBK）。东财 push2 对本机 IP 有间歇限流，故不作主源。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
CAND_FILE = os.path.join(OUT_DIR, "candidates.json")
CACHE_FILE = os.path.join(OUT_DIR, "auction_cache.json")

# GitHub runner 是 UTC，必须显式用北京时间，否则窗口判断全错
BJ = timezone(timedelta(hours=8))

HDR_TX = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

CHG_FLOOR = -3.0        # 条件① 竞价底线
AMT_NEED = 3.0          # 条件② 开盘后 10 分钟成交额门槛（亿）
BREADTH_NEED = 2        # 条件③ 主线翻红家数门槛
DILUTE_WARN = 0.01      # 竞价额/昨日成交额 低于 1% 视为空量
DILUTE_OK = 0.02        # 高于 2% 视为有承接


def http_get(url, headers=None, retries=3, timeout=15, encoding="gbk"):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or HDR_TX)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(encoding, errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.0 * (i + 1))
    raise last


def tx_quote(codes, retries=2, timeout=10):
    """腾讯批量行情。返回 code -> dict。
    竞价阶段只有 16 只左右，一次请求就能拿完，重试次数压到 2 次，最坏 3 秒内出结果。"""
    out = {}
    for i in range(0, len(codes), 40):
        batch = codes[i:i + 40]
        q = ",".join(("sh" if c.startswith("6") else "sz") + c for c in batch)
        try:
            txt = http_get(f"https://qt.gtimg.cn/q={q}", retries=retries, timeout=timeout)
        except Exception:
            continue
        for line in txt.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            try:
                f = line.split("=", 1)[1].strip('"').split("~")
                if len(f) < 46:
                    continue
                price = float(f[3] or 0)
                prev = float(f[4] or 0)
                if price <= 0:          # 竞价无成交时退回昨收
                    price = prev
                out[f[2]] = {
                    "name": f[1],
                    "price": price,
                    "prev": prev,
                    "open": float(f[5] or 0),
                    "vol": float(f[6] or 0),
                    "amt": float(f[37] or 0) / 1e4,   # 成交额，亿
                    "hs": float(f[38] or 0),
                    "mcap": float(f[44] or 0),
                    "ts": f[30],
                }
            except Exception:
                continue
        time.sleep(0.15)
    return out


def breadth(q, basket):
    """按行业分组统计篮子温度。返回 {行业: {n, up, strong}}"""
    groups = {}
    for b in basket:
        it = q.get(b["code"])
        if not it or not it["prev"]:
            continue
        chg = (it["price"] / it["prev"] - 1) * 100
        g = groups.setdefault(b.get("hy") or "其他", {"n": 0, "up": 0, "strong": 0})
        g["n"] += 1
        if chg >= 0:
            g["up"] += 1
        if chg >= 3:
            g["strong"] += 1
    return groups


def breadth_lines(groups, unit="翻红", mark=True):
    out = ["主线温度："]
    for hy, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
        flag = (" ✅" if g["up"] >= BREADTH_NEED else " ⚠️") if mark else ""
        out.append(f"- {hy} {g['n']} 只：{unit} **{g['up']}** 只，涨超 3% **{g['strong']}** 只{flag}")
    return out


def load_candidates():
    if not os.path.exists(CAND_FILE):
        return None
    with open(CAND_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def bj_now():
    return datetime.now(BJ)


def now_hm():
    return bj_now().strftime("%H:%M")


def check_window(q, lo, hi):
    """校验行情时间戳。非交易时段接口返回的是上一交易日收盘数据，
    若不拦截，会把「全天成交额」当成「竞价成交额」用，直接误判。
    返回 (level, msg)，level 为 "" / "soft" / "hard"。hard 表示数据完全不可用。"""
    today = bj_now().strftime("%Y%m%d")
    for it in q.values():
        ts = it.get("ts") or ""
        if len(ts) != 14:
            continue
        d, hhmm = ts[:8], int(ts[8:12])
        hm = f"{ts[8:10]}:{ts[10:12]}"
        if d != today:
            return "hard", (f"⚠️ 行情日期 {d}，非今日 —— 今日尚未开盘或休市，"
                            f"下列数字是上一交易日收盘口径，不可当竞价用。")
        if not (lo <= hhmm <= hi):
            return "soft", (f"⚠️ 行情时间 {hm}，不在 "
                            f"{lo // 100:02d}:{lo % 100:02d}–{hi // 100:02d}:{hi % 100:02d} "
                            f"窗口内，口径可能不符。")
        return "", ""
    return "", ""


# ---------------------------------------------------------------- auction
def wait_for_auction_close():
    """等到 9:25:20 再取数。撮合前拿到的只是未成交的意向量，竞价额会偏小甚至为 0；
    9:30 之后 f[37] 已混入开盘后的成交量，竞价额又会偏大。所以取数窗口很窄。"""
    t = bj_now()
    if t.hour != 9:
        return
    target = t.replace(minute=25, second=20, microsecond=0)
    wait = (target - t).total_seconds()
    if 0 < wait <= 700:
        print(f"等待集合竞价撮合，{wait:.0f} 秒后取数…", flush=True)
        time.sleep(wait)


def run_auction():
    wait_for_auction_close()
    data = load_candidates()
    if not data:
        return "未找到 out/candidates.json，请先运行 scan.py 生成当日名单。"

    cands = data.get("candidates") or []
    basket = [b for b in (data.get("basket") or [])
              if b["code"] not in {c["code"] for c in cands}]
    codes = [c["code"] for c in cands] + [b["code"] for b in basket]
    if not codes:
        return "当日无 2 板及以上断板标的，无需竞价判断。"

    q = tx_quote(codes)
    hm = now_hm()

    lines = [f"**竞价判定 {hm}**　基准日 {fmt_date(data.get('date'))}"]
    level, warn = check_window(q, 920, 935)
    if warn:
        lines += ["", warn]
    lines.append("")
    cache = {}

    if cands:
        lines.append("| 代码 | 名称 | 竞价 | 竞价额 | 占昨额 | 条件① | 提示 |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in cands:
            it = q.get(c["code"])
            if not it:
                lines.append(f"| {c['code']} | {c['name']} | — | — | — | 取数失败 | — |")
                continue
            chg = (it["price"] / it["prev"] - 1) * 100 if it["prev"] else 0
            amt = it["amt"]
            ratio = amt / c["amt"] if c.get("amt") else 0

            ok1 = chg >= CHG_FLOOR
            if level == "hard":
                tip = "—"
            elif ratio < DILUTE_WARN:
                tip = "空量，承接存疑"
            elif ratio >= DILUTE_OK:
                tip = "量能达标"
            else:
                tip = "量能偏弱"

            if level == "hard":
                pass
            elif not ok1:
                tip = "低开超限，放弃"
            elif chg >= 2:
                tip = "高开强，" + tip
            elif chg >= 0:
                tip = "平开，" + tip
            else:
                tip = "低开，" + tip

            lines.append(
                f"| {c['code']} | {c['name']} | {chg:+.2f}% | {amt:.2f}亿 | "
                f"{ratio * 100:.1f}% | "
                f"{'—' if level == 'hard' else ('✅' if ok1 else '❌')} | {tip} |"
            )
            cache[c["code"]] = {"chg": chg, "amt": amt, "ratio": ratio}

    # 主线温度
    if basket:
        lines.append("")
        lines += breadth_lines(breadth(q, basket), unit="竞价翻红", mark=(level != "hard"))

    lines.append("")
    lines.append("条件②③（10 分钟成交额、真实翻红家数）要到 9:41 才能判，届时跑 confirm。")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"time": hm, "items": cache}, fh, ensure_ascii=False, indent=1)
    return "\n".join(lines)


# ---------------------------------------------------------------- confirm
def run_confirm():
    data = load_candidates()
    if not data:
        return "未找到 out/candidates.json，请先运行 scan.py。"

    cands = data.get("candidates") or []
    if not cands:
        return "当日无 2 板及以上断板标的。"

    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as fh:
                cache = (json.load(fh) or {}).get("items") or {}
        except Exception:
            cache = {}

    basket = [b for b in (data.get("basket") or [])
              if b["code"] not in {c["code"] for c in cands}]
    codes = [c["code"] for c in cands] + [b["code"] for b in basket]
    q = tx_quote(codes)
    hm = now_hm()

    # 主线温度（按行业分组，每只股只看自己所在板块）
    groups = breadth(q, basket)

    lines = [f"**开盘承接判定 {hm}**　基准日 {fmt_date(data.get('date'))}"]
    level, warn = check_window(q, 938, 952)
    if warn:
        lines += ["", warn]
    if not cache:
        lines += ["", "⚠️ 未找到 9:26 的竞价快照，成交额按全天累计计，会高估承接。"]
    lines.append("")
    lines.append("| 代码 | 名称 | 现价 | 涨幅 | 开盘后成交 | 条件② | 条件③ | 结论 |")
    lines.append("|---|---|---|---|---|---|---|---|")

    for c in cands:
        it = q.get(c["code"])
        if not it:
            lines.append(f"| {c['code']} | {c['name']} | — | — | — | 取数失败 | — | — |")
            continue
        chg = (it["price"] / it["prev"] - 1) * 100 if it["prev"] else 0
        auc_amt = (cache.get(c["code"]) or {}).get("amt", 0) or 0
        open_amt = max(0.0, it["amt"] - auc_amt)

        ok1 = chg >= CHG_FLOOR
        ok2 = open_amt >= AMT_NEED
        g = groups.get(c.get("hy") or "其他") or {"up": 0}
        ok3 = g["up"] >= BREADTH_NEED

        if level == "hard":
            verdict = "—"
        elif ok1 and ok2 and ok3:
            verdict = "**可进**"
        elif ok1 and ok2:
            verdict = "个股合格，板块未起"
        elif not ok2:
            verdict = "量不够，放弃"
        else:
            verdict = "放弃"

        lines.append(
            f"| {c['code']} | {c['name']} | {it['price']:.2f} | {chg:+.2f}% | "
            f"{open_amt:.2f}亿 | "
            f"{'—' if level == 'hard' else ('✅' if ok2 else '❌')} | "
            f"{'—' if level == 'hard' else ('✅' if ok3 else '❌')} | {verdict} |"
        )

    lines.append("")
    lines += breadth_lines(groups, unit="翻红", mark=(level != "hard"))
    lines.append("")
    lines.append(f"条件③需同板块 ≥{BREADTH_NEED} 只翻红。口径：开盘后成交 = 当前累计成交额 "
                 f"− 9:25 竞价成交额；条件②门槛 {AMT_NEED} 亿，条件①竞价底线 {CHG_FLOOR}%。")
    return "\n".join(lines)


def fmt_date(d):
    if not d or len(d) != 8:
        return "—"
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# ---------------------------------------------------------------- push
def push(title, body):
    url = os.getenv("WEBHOOK_URL", "").strip()
    if not url:
        return
    text = body.replace("**", "")
    try:
        if "sctapi.ftqq.com" in url:            # Server酱
            data = urllib.parse.urlencode({"title": title, "desp": body}).encode()
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/x-www-form-urlencoded"})
        elif "qyapi.weixin.qq.com" in url:      # 企业微信机器人
            data = json.dumps({"msgtype": "markdown",
                               "markdown": {"content": f"**{title}**\n{body}"}},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json"})
        elif "oapi.dingtalk.com" in url:        # 钉钉机器人
            data = json.dumps({"msgtype": "markdown",
                               "markdown": {"title": title, "text": f"### {title}\n{body}"}},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json"})
        elif "open.feishu.cn" in url:           # 飞书机器人
            data = json.dumps({"msg_type": "text",
                               "content": {"text": f"{title}\n{text}"}},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json"})
        else:
            data = json.dumps({"text": f"{title}\n{text}"},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001
        print(f"推送失败（不影响输出）：{e}")


def run_stage(stage):
    stage = (stage or "auction").lower()
    body = run_auction() if stage == "auction" else run_confirm()
    print(body)
    title = f"竞价判定 {now_hm()}" if stage == "auction" else f"开盘承接 {now_hm()}"
    push(title, body)
    return body


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run_stage(sys.argv[1] if len(sys.argv) > 1 else "auction")


if __name__ == "__main__":
    main()
