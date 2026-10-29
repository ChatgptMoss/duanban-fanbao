#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一入口，按北京时间自动选择要跑的阶段。

    python run.py            # auto：按当前时间判断
    python run.py scan       # 强制跑盘前名单
    python run.py auction    # 强制跑竞价判定
    python run.py confirm    # 强制跑开盘承接

auto 的用意：GitHub cron 不保证准点，可能提前或延迟十几分钟。
按实际到达时间选阶段，比按 cron 表达式猜要稳。
"""

import sys
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))


def auto_stage():
    """按实际到达时间选阶段。GitHub cron 只会延后不会提前，所以窗口按「不早于」设计。

    9:30 是竞价阶段的上限：再晚 f[37] 已混入开盘成交量，竞价额就不准了，
    与其给出错的竞价判定，不如直接跳到承接判定。
    """
    hm = int(datetime.now(BJ).strftime("%H%M"))
    if 920 <= hm <= 930:
        return "auction"
    if 931 <= hm <= 955:
        return "confirm"
    return "scan"


def ensure_candidates():
    """竞价/承接阶段依赖盘前生成的名单。8:30 那次若失败，这里补跑一次。"""
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "out", "candidates.json")
    if os.path.exists(p):
        age = datetime.now(BJ).timestamp() - os.path.getmtime(p)
        if age < 20 * 3600:
            return
    print("[run] 当日名单缺失或过期，先补跑 scan", flush=True)
    import scan
    scan.main()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    arg = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    stage = auto_stage() if arg in ("auto", "") else arg
    print(f"[run] stage={stage}  beat={datetime.now(BJ):%H:%M:%S}", flush=True)

    if stage == "scan":
        import scan
        scan.main()
    else:
        ensure_candidates()
        import live
        live.run_stage(stage)


if __name__ == "__main__":
    main()
