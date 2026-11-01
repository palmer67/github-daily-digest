# GitHub 每日开源速递

<p>
  <a href="https://github.com/palmer67/github-daily-digest/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/palmer67/github-daily-digest/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12-blue">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-green"></a>
  <img alt="每天 07:30" src="https://img.shields.io/badge/%E6%AF%8F%E5%A4%A9-07%3A30-07ea5a">
</p>

每天早上自动挑 25 个值得看的开源项目，用中文讲清楚**是什么 / 有什么用 / 一个月花多少钱 / 有什么坑**，推到企业微信群。

不用打开 GitHub 翻榜单，也不用自己写介绍。

## 它长什么样

一条消息一个栏目，每个项目一张卡：

```markdown
## 今日热门（2026-11-02）

### 1. thedotmack/claude-mem　⭐108.6k · 今日 +1527 · TypeScript
**是什么**：给 Claude Code 用的长期记忆插件，自动把会话里的关键信息存成可检索的笔记。
**价值**：跨会话记住项目约定，不用每次重新交代背景。
**成本**：免费 · 要 API Key · 不用起服务 · 不用显卡
**坑**：首次索引历史会话比较慢，大仓库要等几分钟。
[👉 看项目](https://github.com/thedotmack/claude-mem)
```

## 特性

- **5 个栏目**：今日热门（trending 榜单）、新兴潜力（新仓库里 star 涨得快的）、dsh 生态（按 topic 收）、开源工作台（agent/workbench 类）、羊毛专区（免费额度类服务）。
- **先便宜后贵的过滤**：只花榜单自带字段就能筛掉没简介、没主语言、清单仓库、star 与 fork 比例异常的；再把同批刷量仓库（简介一字不差）收敛成一条；最后才读 README、调模型。
- **反胡编**：卡片固定五段，每个成本字段必须能指回 README 原文，指不回来就写「未说明」；难度按 README 里的技术信号做兜底，只上调不下调；README 只当资料，里面的指令不执行。
- **按卡片边界切消息**：企业微信 markdown 上限 4096 字节，按 4000 字节切、只在 `### ` 卡片边界切，不会把一张卡劈成两半。
- **去重与补位**：状态文件记最近一周推过谁；不足 5 条就顺着同一查询往下补；补不齐就在消息里写明「今日仅 N 条」，不拿历史凑数。
- **失败看得见**：推送结果不看 HTTP 状态码，只看企业微信返回的 `errcode`（HTTP 200 也可能是失败）；key 失效时整批降级成原始简介并单独发告警，任务标红；推送失败不写心跳。
- **零服务器**：全部跑在 GitHub Actions 免费额度上，每天几十次模型调用，一个月两三块钱。

## 快速开始

1. **Fork / Clone** 本仓库。
2. **拿两个东西**：企业微信内部群的机器人 Webhook（群设置 → 群机器人 → 添加）、DeepSeek 的 API Key。
3. **配 Secrets**（Settings → Secrets and variables → Actions）：

   | Name | 必填 | 说明 |
   | --- | --- | --- |
   | `DEEPSEEK_API_KEY` | ✅ | 生成摘要用 |
   | `WECOM_WEBHOOK_URL` | ✅ | 企业微信群机器人地址 |
   | `HEARTBEAT_URL` | ⬜ | 可选，外部心跳（如 healthchecks.io），企微整体挂掉时能收到邮件 |

   `GITHUB_TOKEN` 由 Actions 自动注入，不用建。

4. **手动跑一次**：Actions → `每日速递 · 定时投递与监控` → Run workflow。群里应该收到 5 条消息，`state/sent.json` 会被 bot 自动提交。
5. **等它自己跑**：默认每天 07:30 左右触发，另有 08:22 的备用入口（主跑成功时秒退，不重复推送）。

本地试跑（不发群）：

```bash
python -m pip install -r requirements.txt
export GITHUB_TOKEN=$(gh auth token)        # Windows: set GITHUB_TOKEN=...

python scripts/collect.py                            # 只采集，产出 build/candidates-<date>.json
python scripts/summarize.py --offline --limit 5      # 用离线桩跑通管道，不花钱
python scripts/push.py --cards build/cards-<date>.json   # 只渲染，不发送
python scripts/daily.py --dry-run                    # 全流程走一遍，不推送、不写状态
```

## 工作原理

```mermaid
flowchart LR
    A["采集<br/>榜单页 / Search API"] --> B["便宜过滤<br/>无简介 / 无语言 / 清单仓库"]
    B --> C["同批重复收敛<br/>简介近似只留一条"]
    C --> D["读 README<br/>raw 优先，API 兜底"]
    D --> E["大模型生成卡片<br/>五段 + 原文可追溯"]
    E --> F["去重补位<br/>7 天窗口 + 顺延"]
    F --> G["渲染切分<br/>4000 字节，不劈卡"]
    G --> H["企业微信<br/>判 errcode"]
    F -.-> S[("state/sent.json")]
    H --> S
```

一次运行大约一分钟：采集（几十次 GitHub 请求）→ 过滤 → 摘要（几十次模型调用）→ 推送。每张卡片都会带上数据来源，方便事后核对。

## 配置

全部在 [`config/columns.yaml`](config/columns.yaml)，改完提交即可，不用碰代码。字段拼错、缺字段、重复 id 会在启动时就报中文错误，不会静默用默认值。

| 字段 | 作用 |
| --- | --- |
| `global.items_per_column` | 每个栏目推几条（默认 5） |
| `global.dedupe_days` | 去重窗口，默认 7 天；榜单栏目可单独收紧 |
| `global.backfill` | 不足时是否顺延补位 |
| `global.max_message_bytes` | 单条消息字节上限，默认 4000 |
| `global.llm` | 模型、温度、超时、重试退避 |
| `filters.*` | 质量过滤与重复判定阈值 |
| `columns[].enabled` | 关掉的栏目连查询都不会发 |

加一个栏目：

```yaml
  - id: rust
    name: Rust 新项目
    enabled: true
    source: github_search
    query: "language:rust stars:>100 created:>{30d}"
    sort: stars
    pool: 40
    max_items: 5
```

`{30d}` 会被替换成 30 天前的日期，所以查询不用每天手改。

## 成本

| 项目 | 每天 | 每月 |
| --- | --- | --- |
| 模型调用（约 25 张卡） | 约 0.06 元 | 约 2 元 |
| GitHub Actions | 0（公开仓库免费） | 0 |
| 服务器 | 无 | 无 |

想更省：把 `items_per_column` 调到 3，或关掉不关心的栏目。

## 项目结构

```
config/columns.yaml        栏目与全局配置
src/digest/
  collectors/              榜单页解析、Search API 采集
  filters.py               质量过滤 + 同批重复判定（纯函数）
  readme.py                README 获取与裁剪
  summarizer/              提示词、JSON 校验、难度兜底、客户端
  renderer.py              卡片渲染与按边界切分
  notifiers/wecom.py       企业微信推送与 errcode 判定
  daily.py                 每日编排：幂等 → 采集 → 摘要 → 推送 → 写状态
state/                     去重记录、运行记录、心跳（程序自动提交）
scripts/                   collect / summarize / push / daily 等入口
tests/                     单元测试
```

## 已知限制

- 卡片质量靠「白名单 + 难度兜底 + 降级卡」，没有人工复核环节。
- README 只读开头 3000 字 + 安装段 3000 字，超长 README 的中段（常见 changelog）不参与判断。
- trending 靠 HTML 正则解析，页面改版会解析到 0 条并直接报错，不会静默推空栏目。
- 企业微信**外部群不支持**群机器人，必须是企业内部群。
- 「同批重复」只比简介文本，能抓一字不差的刷量仓库；语义聚类（同一天冒出来的几个同义项目）没做。
- dsh 生态栏目按最近更新排序，会收到 star 很少的新插件——这是有意的取舍：按 star 排会被头部项目长期占位。

## Roadmap

- [x] 采集 / 摘要 / 推送 / 去重 / 定时 / 监控全链路
- [x] 推送失败与 key 失效的降级和告警
- [ ] 语义级去重（embedding 聚类）
- [ ] 卡片配图（仓库截图或 og:image）
- [ ] 多渠道路由（飞书、邮件、Telegram）

## License

[MIT](LICENSE)
