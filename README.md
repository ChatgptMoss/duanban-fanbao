# 断板反包 · 每日名单与盘中判定

A 股 2 板及以上断板股的盘前筛选，以及次日竞价的实时判定。

## 三个时间点

| 北京时间 | 阶段 | 内容 |
|---|---|---|
| 21:30 | `scan` | 断板反包池：2 板及以上断板、主板、换手 ≥10%、跌幅 ≥ -5%，标龙位 |
| 09:25 | `auction` | 竞价判定：竞价涨幅、竞价额占昨日成交额比、板块竞价翻红数 |
| 09:40 | `confirm` | 承接判定：开盘后 10 分钟成交额、板块真实翻红家数、最终结论 |

名单放在晚上算，不跟竞价抢时间，允许重试；9:25 那次只读现成名单，一次 HTTP 请求。

竞价阶段会在 9:15–9:25 之间自动等到 9:25:20 再取数，避免拿到未撮合的意向量。

## 龙位口径

断板池按「龙头优先级」排序并标注龙 N：

```
连板高度高 → 首封时间早 → 封单大 → 炸板次数少
```

即游资通用口径：高度定龙头；高度相同看谁先封死；再同看封单与封板质量。

- `龙1` = 池内优先级最高的那只，依次龙 2、龙 3
- `龙1·3` = 池内第 1，且所属板块当日涨停 3 只（有梯队）
- 不带后缀 = 所属板块当日只有它 1 只涨停，属于独苗，没有带动性

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

3. Settings → Secrets and variables → Actions，添加推送地址（可多选，配了哪个就推哪个）：

| Secret 名 | 渠道 | URL 形态 |
|---|---|---|
| `WEBHOOK_URL` | 企业微信机器人 / Server酱 / 钉钉 / 飞书 | 见下表 |
| `WECOM_WEBHOOK` | 企业微信群机器人 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` |
| `DINGTALK_WEBHOOK` | 钉钉机器人 | `https://oapi.dingtalk.com/robot/send?access_token=...` |
| `FEISHU_WEBHOOK` | 飞书机器人 | `https://open.feishu.cn/open-apis/bot/v2/hook/...` |

`WEBHOOK_URL` 支持按域名自动识别，也可用逗号分隔填多个地址；其余三个是等价的显式别名。全部不填则只写文件、不推送。

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
