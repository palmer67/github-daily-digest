# GitHub 每日开源速递

每天早上把 GitHub 上的热门与新兴开源项目，用通俗中文总结（是什么 / 有什么用 / 多少成本），按栏目推送到企业微信内部群。

> 当前进度：**M0 已通过（2026-11-01，GitHub Actions 云端 5/5）**，下一步 M1 采集层。M1 及以后按《项目策划书 v1.1》第 8 节逐项推进。

---

## M0 是什么、为什么必须先做

本地（中国大陆出口）已经全部跑通，但 GitHub Actions 的 runner 在美国机房，去 GitHub API、DeepSeek、企业微信是**另一条网络路径**，本机结果不能替代云端验证。M0 就是在云端把这三次往返各跑一遍，并且把两种最危险的失败形态亲手制造出来看一遍：

| 检查 | 期望 | 失败后果 |
| --- | --- | --- |
| GitHub API（已认证） | HTTP 200，core 配额 > 0 | 采集层拿不到数据 |
| DeepSeek API | HTTP 200，返回 usage token | 摘要层不可用 |
| 企业微信 webhook | HTTP 200 **且 body 里 `errcode == 0`** | 只看 HTTP 200 会把失败当成功（静默失败） |
| 故障注入 A：错误 webhook key | HTTP 200 + `errcode 93000` | 证明必须判 errcode |
| 故障注入 B：错误 DeepSeek key | HTTP 401 | 证明 401 要短路，不能重试 3 次 |

`scripts/m0_probe.py` 五项全过才退出码 0；任一项不过，M0 不通过，不进入 M1。

## 跑一次 M0

1. 仓库 Settings → Secrets and variables → Actions 里配置三个 Secret：

   | Secret | 内容 |
   | --- | --- |
   | `DEEPSEEK_API_KEY` | DeepSeek API Key |
   | `WECOM_WEBHOOK_URL` | 企业微信群机器人完整 webhook 地址（含 `?key=`） |
   | （自动）`GITHUB_TOKEN` | Actions 自动注入，无需手工配置 |

   `HEARTBEAT_URL` 属于 M5，M0 不需要。

2. 触发：

   ```bash
   gh workflow run m0-connectivity.yml
   gh run watch
   ```

   或网页 Actions → “M0 连通性冒烟” → Run workflow。

3. 在日志里核对这几行（验收证据）：

   ```
   errcode = 0 → 判定：成功（errcode == 0）
   EVIDENCE wecom_errcode = 0
   EVIDENCE bad_wecom_status = 200
   EVIDENCE bad_wecom_errcode = 93000
   EVIDENCE bad_deepseek_status = 401
   EVIDENCE deepseek_ms = ...
   EVIDENCE deepseek_total_tokens = ...
   M0 RESULT: PASS (5/5)
   ```

   GitHub 上这条消息之所以能进群，说明云端去企业微信的出口是通的；`errcode == 0` 才是判定依据，HTTP 200 不算。

## M0 验收记录（2026-11-01，云端）

首跑即通过：run [41312459131](https://github.com/palmer67/github-daily-digest/actions/runs/41312459131)，runner `ubuntu-26.04`（Azure westus2，美国机房）。

| 检查 | 实测结果 |
| --- | --- |
| GitHub API | HTTP 200，core 配额 5000/5000，`GITHUB_TOKEN` 已认证 |
| DeepSeek API | HTTP 200，717 ms；prompt 25 / completion 1 / total 26 tokens；`thinking=disabled` 被接受；`/models` 返回 `deepseek-flash`、`deepseek-v4-pro` |
| 企业微信 webhook | HTTP 200，body `{"errcode":0,"errmsg":"ok"}`，**errcode == 0 → 判定成功** |
| 故障注入 A：错误 webhook key | **HTTP 200 + `errcode 93000`**（与策划书 A-10 一致，证明只看状态码会误判成功） |
| 故障注入 B：错误 DeepSeek key | **HTTP 401 `authentication_error`**（与 A-11 一致，证明 401 必须短路、不能重试） |

日志锚点：`M0 RESULT: PASS (5/5)`。企业微信群已实收测试消息；日志与运行摘要里 webhook key、DeepSeek key 均被掩码，对仓库工作树的密钥扫描无命中。

## 密钥纪律

- 三个密钥只存在于 **GitHub Secrets** 和运行时的环境变量里。
- 脚本日志对 webhook key、DeepSeek key 一律做掩码（`key=***`、`key=<secret>`），失败信息也只打印 body 摘要。
- 仓库里没有任何 `.env`、密钥文件或密钥字符串；`.gitignore` 只是兜底，不承担安全职责。

## 目录

```
.github/workflows/m0-connectivity.yml   M0 云端冒烟 workflow（仅手动触发）
scripts/m0_probe.py                     五项检查 + 故障注入，标准库实现
probe/                                  本机实测用的原始探针（参考实现，不参与 Actions）
审核报告.md                             策划书 v1.1 的实测审核报告
```

`probe/` 里的 `e2e.py`、`wecom_limits.py`、`adversarial.py` 是策划书附录 A 的实测来源，保留作参照；它们同样只从环境变量读密钥。

## 已知限制（M0 阶段）

- 只验证连通性，不做任何采集、摘要、去重、状态提交。
- workflow 仅 `workflow_dispatch` 触发，避免每次 push 都往群里发测试消息。
- 企业微信外部群不支持群机器人，必须使用**企业内部群**的机器人。
