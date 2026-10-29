#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2 板及以上断板股筛选
用途：每个交易日盘前输出一份断板名单，供 9:25 集合竞价决策。

筛选口径：
  1. 前一交易日涨停池中「连板数 >= 2」的股票，减去当日涨停池 → 断板
  2. 只保留主板（60/00 开头，剔除 688/300/301/920）
  3. 当日换手率 >= 10%
  4. 当日涨跌幅 >= -5%
  5. 只看当日断板，不做跨日跟踪

数据源：东方财富涨停池/炸板池 + 腾讯行情（换手/涨幅/流通市值）+ 东财资金流（主力净额）
"""

import datetime
import json
import os
import smtplib
import sys
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText

UT = "7eea3edcaed734bea9cbfc24409ed989"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
OUT_FILE = os.path.join(OUT_DIR, "latest.md")

HDR_EM = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
HDR_TX = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}
HDR_SINA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def http_get(url, headers=None, retries=3, timeout=25, encoding="utf-8"):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or HDR_EM)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode(encoding, errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def _pool(kind, date):
    """kind: ZT(涨停) / ZB(炸板)"""
    url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool?ut={UT}&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=500&sort=fbt%3Aasc&date={date}")
    try:
        d = json.loads(http_get(url, retries=2))
    except Exception:
        return None
    return d.get("data")


def trading_days(n=2):
    """取最近 n 个交易日（最新在前）。盘前运行时最后一根K线就是上一交易日，节假日自动跳过。"""
    # 主源：腾讯日K
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param=sh000001,day,,,{n + 3},qfq")
        d = json.loads(http_get(url, headers=HDR_TX, retries=3))
        rows = d["data"]["sh000001"].get("day") or d["data"]["sh000001"].get("qfqday") or []
        ds = [r[0].replace("-", "") for r in rows]
        if len(ds) >= n:
            return ds[-n:][::-1]
    except Exception:
        pass
    # 兜底：东财日K
    try:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
               f"&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&end=20500101&lmt={n + 3}")
        d = json.loads(http_get(url, retries=2))
        ds = [k.split(",")[0].replace("-", "") for k in d["data"]["klines"]]
        if len(ds) >= n:
            return ds[-n:][::-1]
    except Exception:
        pass
    return []


def resolve_days(retry=2):
    """返回 (当日日期, 当日data, 前一日日期, 前一日data)

    晚上跑不抢时间，允许重试：接口偶发返回空数据或未更新完的涨停池，
    若直接把空名单推给次日竞价，整条链路就废了。
    """
    for attempt in range(retry + 1):
        ds = trading_days(2)
        if len(ds) >= 2:
            d1, d2 = ds[0], ds[1]
            p1, p2 = _pool("ZT", d1), _pool("ZT", d2)
            # tc=涨停家数。少于 5 家基本可判定为数据未更新完整
            if p1 and p2 and (p1.get("tc") or 0) > 5:
                return d1, p1, d2, p2
        if attempt < retry:
            time.sleep(20)
    return None


def is_main_board(code):
    return code[:2] in ("60", "00") and not code.startswith("688")


def tencent_quotes(codes):
    """腾讯行情：换手率、涨跌幅、成交额、流通市值"""
    out = {}
    for i in range(0, len(codes), 40):
        batch = codes[i:i + 40]
        q = ",".join(("sh" if c.startswith("6") else "sz") + c for c in batch)
        try:
            txt = http_get(f"https://qt.gtimg.cn/q={q}", headers=HDR_TX, encoding="gbk")
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
                out[f[2]] = {
                    "name": f[1],
                    "chg": float(f[32] or 0),
                    "hs": float(f[38] or 0),
                    "amt": float(f[37] or 0) / 10000.0,   # 成交额，亿
                    "mcap": float(f[44] or 0),            # 流通市值，亿
                }
            except Exception:
                continue
        time.sleep(0.2)
    return out


def eastmoney_fund(codes):
    """东财主力净额（元）。push2his 域名对非浏览器客户端更宽容，push2 兜底。"""
    out = {}
    hosts = ("push2his.eastmoney.com", "push2.eastmoney.com")
    for i in range(0, len(codes), 15):
        batch = codes[i:i + 15]
        secids = ",".join(("1." if c.startswith("6") else "0.") + c for c in batch)
        path = (f"/api/qt/ulist.np/get?fltt=2&secids={secids}"
                f"&fields=f12,f62&ut={UT}")
        for host in hosts:
            try:
                d = json.loads(http_get("https://" + host + path, retries=3, timeout=20))
                for it in ((d.get("data") or {}).get("diff") or []):
                    v = it.get("f62")
                    if v is not None:
                        out[it["f12"]] = float(v)
                break
            except Exception:
                time.sleep(1.0)
                continue
        time.sleep(0.4)
    return out


def sina_fund(codes):
    """新浪资金净额兜底（元）。口径是「主动买 − 主动卖」，与东财「大单净额」不同，
    输出时用 * 标记，避免两种口径混作一列。东财限流时至少还有个可用的数。"""
    out = {}
    for c in codes:
        sym = ("sh" if c.startswith("6") else "sz") + c
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"MoneyFlow.ssi_ssfx_flzjtj?daima={sym}")
        try:
            d = json.loads(http_get(url, headers=HDR_SINA, retries=2, timeout=12))
            v = d.get("netamount")
            if v is not None:
                out[c] = float(v)
        except Exception:
            pass
        time.sleep(0.25)
    return out


def watch_point(fund, chg, amt):
    """生成竞价观察点"""
    if fund is None:
        mood = "高开≥+1%" if chg >= 3 else ("-1%~+3% 正常" if chg >= 0 else "需翻红")
    elif fund > 0:
        mood = "高开≥+2%且9:20后不撤单" if chg >= 3 else "-1%~+3% 属正常"
    else:
        mood = "主力净流出，需翻红才看"
    vol = amt * 0.02
    vol_s = f"{vol:.2f}".rstrip("0").rstrip(".") if vol >= 0.1 else "0.1"
    return f"{mood}｜竞价额≥{vol_s}亿"


def build_basket(p_today, p_prev, candidates):
    """主线篮子 = 前一交易日涨停池中，与候选股同行业的股票。

    只取 p_prev 不取 p_today：当日新涨停的股次日竞价天然高开，混进来会把
    「板块温度」算虚高。要看的其实是「昨天同板块一起涨停的，今天还有几只被接住」。
    """
    hys = {r["hy"] for r in candidates if r.get("hy")}
    seen = {}
    for x in (p_prev or {}).get("pool") or []:
        h = x.get("hybk") or ""
        if h in hys:
            seen[x["c"]] = (x["n"], h)
    for r in candidates:
        seen[r["code"]] = (r["name"], r.get("hy") or "")
    return [{"code": c, "name": n, "hy": h} for c, (n, h) in seen.items()]


def build():
    days = resolve_days()
    if not days:
        return "数据获取失败：涨停池接口无返回，请检查网络或接口变更。", None, []
    d_today, p_today, d_prev, p_prev = days

    today_codes = {x["c"] for x in p_today.get("pool") or []}
    zb = _pool("ZB", d_today)
    zb_codes = {x["c"] for x in (zb or {}).get("pool") or []}

    # 断板 = 前一日连板数>=2 且 当日未涨停
    broken = [x for x in (p_prev.get("pool") or [])
              if x["lbc"] >= 2 and x["c"] not in today_codes]

    row_notes = []          # 被剔除的
    candidates = []
    quotes = tencent_quotes([x["c"] for x in broken]) if broken else {}

    for x in broken:
        code, name, lbc = x["c"], x["n"], x["lbc"]
        if not is_main_board(code):
            row_notes.append((code, name, "非主板"))
            continue
        q = quotes.get(code)
        if not q:
            row_notes.append((code, name, "行情获取失败"))
            continue
        if q["hs"] < 10:
            row_notes.append((code, name, f"换手 {q['hs']:.2f}% 不足"))
            continue
        if q["chg"] < -5:
            row_notes.append((code, name, f"跌 {q['chg']:.2f}% 破位"))
            continue
        pos = f"{lbc}板→{lbc + 1}板炸" if code in zb_codes else f"{lbc}板断"
        candidates.append({"code": code, "name": name, "pos": pos,
                           "chg": q["chg"], "hs": q["hs"], "amt": q["amt"],
                           "mcap": q["mcap"], "lbc": lbc,
                           "hy": x.get("hybk") or ""})

    candidates.sort(key=lambda r: (-r["lbc"], -r["hs"]))
    # 只有东财口径参与判定，新浪只作显示兜底：两者量级差近 10 倍，
    # 混用会把「高开≥+2%」这类门槛抬得过高
    funds = eastmoney_fund([r["code"] for r in candidates]) if candidates else {}
    for r in candidates:
        r["fund"] = funds.get(r["code"])
    miss = [r["code"] for r in candidates if r["fund"] is None]
    alt = sina_fund(miss) if miss else {}
    for r in candidates:
        r["fund_alt"] = alt.get(r["code"])

    basket = build_basket(p_today, p_prev, candidates)
    payload = {"date": d_today, "prev_date": d_prev,
               "candidates": candidates, "basket": basket}

    lines = [f"**数据基准：{d_today[:4]}-{d_today[4:6]}-{d_today[6:]} 收盘"
             f"（对比前一交易日 {d_prev[:4]}-{d_prev[4:6]}-{d_prev[6:]}）**", ""]

    if candidates:
        lines.append("| 代码 | 名称 | 身位 | 当日 | 换手 | 主力净额 | 流通 | 竞价观察点 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        missing = 0
        used_alt = False
        for r in candidates:
            f, fa = r.get("fund"), r.get("fund_alt")
            if f is not None:
                fs = f"{f / 1e4:+.0f}万"
            elif fa is not None:
                fs = f"{fa / 1e4:+.0f}万*"
                used_alt = True
            else:
                fs = "—"
                missing += 1
            lines.append(
                f"| {r['code']} | {r['name']} | {r['pos']} | {r['chg']:+.2f}% | "
                f"{r['hs']:.2f}% | {fs} | {r['mcap']:.0f}亿 | "
                f"{watch_point(f, r['chg'], r['amt'])} |"
            )
        if used_alt:
            lines.append("")
            lines.append("注：带 * 的为新浪「主动买−主动卖」净额，量级比东财「大单净额」大，"
                         "只用于看方向，不参与观察点计算。")
        if missing:
            lines.append("")
            lines.append(f"注：{missing} 只资金数据两个源均未取到，不影响筛选条件。")
    else:
        lines.append("今日无符合条件的 2 板及以上断板标的。")

    if row_notes:
        lines.append("")
        reasons = "；".join(f"{n}（{why}）" for _, n, why in row_notes)
        lines.append(f"剔除：{reasons}")

    return "\n".join(lines), d_today, payload


def send_mail(subject, body):
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    to = os.getenv("MAIL_TO")
    if not all([host, user, pwd, to]):
        return
    port = int(os.getenv("SMTP_PORT", "465"))
    msg = MIMEText(body.replace("**", "").replace("|", " "), "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    body, day, payload = build()
    print(body)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    with open(os.path.join(OUT_DIR, "candidates.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")

    if day:
        try:
            send_mail(f"断板反包名单 {day[:4]}-{day[4:6]}-{day[6:]}", body)
        except Exception as e:  # noqa: BLE001
            print(f"邮件发送失败（不影响主流程）：{e}")
        try:
            from live import push
            push(f"断板反包池 {day[4:6]}-{day[6:]}（次一交易日竞价用）", body)
        except Exception as e:  # noqa: BLE001
            print(f"推送失败（不影响主流程）：{e}")


if __name__ == "__main__":
    main()
