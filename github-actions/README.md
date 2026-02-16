# A股三合一多策略选股器 - GitHub Actions 云端版

将三个本地策略（三日反转 + 放量突破 + 缩量突破）迁移到 GitHub Actions 云端定时执行，并利用 Claude Agent SDK 自动生成分析报告。

## 文件结构

```
github-actions/
  combined_screener_cloud.py    # 三合一云端筛选脚本（单文件，含3策略）
  claude_agent_runner.py        # Claude Agent SDK 执行器（tool_use模式）
  requirements.txt              # Python 依赖
  workflows/
    stock-screener-team.yml     # GitHub Actions workflow 定义
  README.md                     # 本文件
```

## 策略说明

| 策略 | 形态 | 核心逻辑 |
|------|------|----------|
| 三日反转 | 小阴线 → 大阴线 → 低开高收阳线 | 下跌趋势末端的反转信号 |
| 放量突破 | 下跌 → 横盘 → 涨停 → 回踩 → 放量突破 | 主力建仓后的二次突破 |
| 缩量突破 | 下跌 → 横盘 → 涨停 → 横盘 → 缩量突破 | 涨停后洗盘完成的启动信号 |

## 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 运行筛选（无需 API Key）
python combined_screener_cloud.py

# 运行 AI 分析（需要 ANTHROPIC_API_KEY）
export ANTHROPIC_API_KEY=your_key_here
python claude_agent_runner.py
```

## 部署到 GitHub Actions

### 1. 复制 workflow 文件

将 `workflows/stock-screener-team.yml` 复制到你的仓库：

```bash
cp workflows/stock-screener-team.yml .github/workflows/
```

### 2. 配置 GitHub Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 必须 | 说明 |
|--------|------|------|
| `ANTHROPIC_API_KEY` | **是** | Anthropic API 密钥 |
| `ANTHROPIC_BASE_URL` | 否 | 自定义 API 地址（第三方代理） |
| `CLAUDE_MODEL` | 否 | 模型ID（默认 claude-sonnet-4-5-20250929） |
| `WECHAT_WEBHOOK` | 否 | 企业微信机器人 Webhook URL |
| `NOTIFICATION_EMAIL` | 否 | 邮件通知收件地址 |
| `SMTP_USERNAME` | 否 | 邮件发送账号 |
| `SMTP_PASSWORD` | 否 | 邮件发送密码 |

### 3. 推送代码

```bash
git add github-actions/ .github/workflows/stock-screener-team.yml
git commit -m "Add stock screener team cloud version"
git push
```

### 4. 手动触发测试

在 GitHub 仓库页面 → Actions → "A股三合一多策略选股器" → Run workflow。

## 定时执行

默认每个交易日北京时间 18:00（UTC 10:00）自动执行，可在 workflow 文件中修改 cron 表达式。

## 输出文件

每次运行产出：

| 文件 | 说明 |
|------|------|
| `reversal_*.csv` | 三日反转策略命中股票 |
| `volume_breakout_*.csv` | 放量突破策略命中股票 |
| `shrink_breakout_*.csv` | 缩量突破策略命中股票 |
| `latest_results.json` | 结构化结果（供 AI 分析） |
| `results.md` | Markdown 汇总报告 |
| `ai_analysis_report.md` | Claude AI 分析报告 |

结果文件会以 Artifact 形式保留 30 天，同时写入 GitHub Actions Summary。

## 与本地版的差异

| 维度 | 本地版 | 云端版 |
|------|--------|--------|
| 数据源-股票列表 | AKShare `stock_zh_a_spot_em()` | BaoStock `query_stock_basic()` |
| 数据源-K线 | AKShare `stock_zh_a_hist()` | BaoStock `query_history_k_data_plus()` |
| 代码结构 | 动态import 3个兄弟目录 | 单文件内嵌3个策略 |
| 并发 | 30线程 ThreadPoolExecutor | 串行遍历（BaoStock限制） |
| 预筛选 | 今日收阳（实时行情） | 跳过（BaoStock无实时接口） |
| AI分析 | 无 | Claude Agent SDK (tool_use) |
| 输出 | CSV + 终端打印 | CSV + JSON + Markdown + GitHub Summary |

## 风险提示

本工具仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。
