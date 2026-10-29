# 断板反包 · 每日名单与盘中判定

A 股 2 板及以上断板股的盘前筛选，以及次日竞价的实时判定。

## 三个时间点

| 北京时间 | 阶段 | 内容 |
|---|---|---|
| 08:30 | `scan` | 盘前名单：2 板及以上断板、主板、换手 ≥10%、跌幅 ≥ -5% |
| 09:26 | `auction` | 竞价判定：竞价涨幅、竞价额占昨日成交额比、板块竞价翻红数 |
| 09:41 | `confirm` | 承接判定：开盘后 10 分钟成交额、板块真实翻红家数、最终结论 |

竞价阶段会在 9:15–9:25 之间自动等到 9:25:20 再取数，避免拿到未撮合的意向量。

## 三条判定条件

1. 竞价涨幅 ≥ -3%（`CHG_FLOOR`）
2. 开盘后 10 分钟成交额 ≥ 3 亿（`AMT_NEED`）
3. 同板块昨日涨停股中 ≥2 只翻红（`BREADTH_NEED`）

三条全满足才出「可进」。

## 数据源

- 涨停池 / 炸板池：东方财富 `push2ex.eastmoney.com`
- 实时行情：腾讯 `qt.gtimg.cn`（GBK）。东财 `push2` 对部分 IP 有间歇限流，故不作主源
- 交易日历：腾讯上证指数日 K，盘前运行自然落在上一交易日，节假日自动跳过

## 部署

1. GitHub 新建空仓库 `duanban-fanbao`（不要勾选 README / .gitignore / license）

2. 推送：

```bash
git remote add origin https://github.com/<用户名>/duanban-fanbao.git
git push -u origin main
```

3. Settings → Secrets and variables → Actions，添加 `WEBHOOK_URL`：

| 渠道 | URL 形态 |
|---|---|
| 企业微信机器人 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` |
| Server酱 | `https://sctapi.ftqq.com/<SendKey>.send` |
| 钉钉机器人 | `https://oapi.dingtalk.com/robot/send?access_token=...` |
| 飞书机器人 | `https://open.feishu.cn/open-apis/bot/v2/hook/...` |

按域名自动识别，不用额外配类型。不填则只写文件、不推送。

4. Actions → 断板反包 → Run workflow，`stage` 填 `scan` 先试一次。

## 关于准点

GitHub 的 cron 不保证准点，高峰期会延迟十几分钟，9:20 那次可能拖到 9:35 之后，竞价窗口就错过了。

`run.py` 会按实际到达时间自动选阶段，错过 auction 就直接跑 confirm，不会跑错。但想要准点推送，建议用下面任一种外部触发：

- **cron-job.org**（免费）：定时 `POST /repos/<owner>/<repo>/actions/workflows/scan.yml/dispatches`，body `{"ref":"main","inputs":{"stage":"auction"}}`，需要带 `workflow` 权限的 PAT
- **本地计划任务**：电脑 9:15 前必开，直接 `python run.py auction`，秒级准点

## 手动运行

```bash
python run.py            # 按当前时间自动选
python run.py scan
python run.py auction
python run.py confirm
```

仅依赖 Python 标准库，无需 `pip install`。

## 输出

- `out/latest.md` —— 盘前名单
- `out/candidates.json` —— 名单的机器可读版，供竞价阶段读取
- `out/auction_cache.json` —— 9:25 竞价快照，供 9:41 计算开盘后成交额
