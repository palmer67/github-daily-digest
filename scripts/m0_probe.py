#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M0 连通性冒烟测试 + 故障注入（跑在 GitHub Actions 云端 runner 上）。

检查 5 项，全过才退出码 0：

  1. GitHub API       已认证 /rate_limit + 读一个真实仓库
  2. DeepSeek API     /models + 一次最小 chat completion（记录耗时与 token）
  3. 企业微信 webhook  真发一条 markdown，必须解析 body 并判定 errcode == 0
  4. 故障注入 A        错误 webhook key → 期望 HTTP 200 + errcode 93000
  5. 故障注入 B        错误 DeepSeek key → 期望 HTTP 401

硬约束：
  * 密钥只从环境变量读取（由 GitHub Secrets 注入），日志里一律掩码；
  * 任何情况下不把密钥写进文件（包括缓存、日志文件）；
  * 只用标准库，避免 Actions 里装依赖引入额外不确定性。

依据：项目策划书 v1.1 第 8 节 M0、附录 A-1/A-2/A-10/A-11。
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# 本地 Windows 控制台默认 GBK，遇到 ⭐ 这类字符会直接崩；Actions 里本来就是 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  老 Python / 被重定向到不支持的对象时忽略
        pass

UA = "github-daily-digest-m0/1.0"
GH_API = "https://api.github.com"
DS_BASE = "https://api.deepseek.com"
WECOM_SEND = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

# 故障注入用的固定无效值（不是真密钥，写死在这里是安全的）
BAD_WECOM_KEY = "00000000-0000-0000-0000-000000000000"
BAD_DS_KEY = "sk-00000000000000000000000000000000"
EXPECTED_BAD_ERRCODE = 93000

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
TIMEOUT = 40

_SECRETS: list[str] = []


# ---------------------------------------------------------------- 日志与掩码

def register_secret(value: str | None) -> None:
    value = (value or "").strip()
    if value and value not in _SECRETS:
        _SECRETS.append(value)


def scrub(text: str) -> str:
    out = text
    for secret in _SECRETS:
        out = out.replace(secret, "<secret>")
    return re.sub(r"(key=)[0-9a-zA-Z_\-]{6,}", r"\1***", out)


def say(msg: object = "") -> None:
    print(scrub(str(msg)), flush=True)


# ---------------------------------------------------------------- HTTP

@dataclass
class Resp:
    status: int
    headers: dict
    body: bytes
    elapsed: float

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:  # noqa: BLE001
            return None


def request(url: str, *, method: str = "GET", headers: dict | None = None,
            json_body: dict | None = None, timeout: float = TIMEOUT) -> Resp:
    hdrs = {"User-Agent": UA}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Resp(r.status, dict(r.headers), r.read(), time.monotonic() - started)
    except urllib.error.HTTPError as e:
        return Resp(e.code, dict(e.headers or {}), e.read(), time.monotonic() - started)
    except Exception as e:  # noqa: BLE001  网络层异常也要记录成一条结果
        return Resp(-1, {}, f"{type(e).__name__}: {e}".encode(), time.monotonic() - started)


def mask_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if "key" in query:
            query["key"] = ["***"]
            # 手拼 query，避免 *** 被 urlencode 成 %2A%2A%2A
            query_str = "&".join(
                f"{k}={v[0]}" for k, v in query.items()
            )
            return urllib.parse.urlunsplit(parts._replace(query=query_str))
        return url
    except Exception:  # noqa: BLE001
        return "<无法解析的 URL>"


def replace_key(url: str, new_key: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    query["key"] = [new_key]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, doseq=True)))


def post_wecom(url: str, markdown: str) -> Resp:
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    return request(url, method="POST", json_body=payload, timeout=TIMEOUT)


# ---------------------------------------------------------------- 结果模型

@dataclass
class Result:
    key: str
    title: str
    ok: bool
    detail: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 检查 1

def check_github() -> Result:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    register_secret(token)
    detail: list[str] = []
    hdrs = {"Accept": "application/vnd.github+json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"

    r = request(f"{GH_API}/rate_limit", headers=hdrs)
    data = r.json() or {}
    core = ((data.get("resources") or {}).get("core") or {})
    search = ((data.get("resources") or {}).get("search") or {})
    detail.append(f"GET /rate_limit → HTTP {r.status}（{r.elapsed * 1000:.0f} ms）")
    if core:
        detail.append(f"core 配额：{core.get('remaining')}/{core.get('limit')} 剩余")
    if search:
        detail.append(f"search 配额：{search.get('remaining')}/{search.get('limit')} 剩余")
    detail.append(f"认证：{'已认证（GITHUB_TOKEN 生效）' if token else '未认证（缺 GITHUB_TOKEN）'}")

    ok = r.status == 200 and bool(token) and int(core.get("remaining") or 0) > 0
    if not token:
        detail.append("缺 GITHUB_TOKEN：Actions 里应注入 secrets.GITHUB_TOKEN；未认证只有 60 次/小时（策划书 6.2）")

    repo = "octocat/Hello-World"
    r2 = request(f"{GH_API}/repos/{repo}", headers=hdrs)
    detail.append(f"GET /repos/{repo} → HTTP {r2.status}（{r2.elapsed * 1000:.0f} ms）")
    if r2.status == 200:
        meta = r2.json() or {}
        detail.append(f"真实仓库读取：{meta.get('full_name')} ⭐{meta.get('stargazers_count')} · {meta.get('language')}")
    else:
        detail.append(f"仓库读取失败：{r2.text[:200]}")
    ok = ok and r2.status == 200

    return Result(
        "github", "GitHub API 连通", ok, detail,
        {"github_limit": core.get("limit"), "github_remaining": core.get("remaining")},
    )


# ---------------------------------------------------------------- 检查 2

def check_deepseek() -> Result:
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    register_secret(key)
    detail: list[str] = []
    if not key:
        return Result("deepseek", "DeepSeek API 连通", False,
                      ["缺少环境变量 DEEPSEEK_API_KEY（应由 GitHub Secrets 注入）"])

    hdrs = {"Authorization": f"Bearer {key}"}
    r = request(f"{DS_BASE}/models", headers=hdrs)
    ids: list[str] = []
    if r.status == 200:
        ids = [m.get("id") for m in (r.json() or {}).get("data", []) if m.get("id")]
    detail.append(f"GET /models → HTTP {r.status}（{r.elapsed * 1000:.0f} ms）| 模型：{', '.join(ids) or '-'}")
    ok = r.status == 200 and bool(ids)
    if not ok:
        detail.append(f"模型列表异常：{r.text[:300]}")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是连通性测试桩，只按用户要求回复，不要解释。"},
            {"role": "user", "content": "只回复两个字：通了"},
        ],
        "temperature": 0,
        "max_tokens": 16,
        "thinking": {"type": "disabled"},
    }
    c = request(f"{DS_BASE}/chat/completions", method="POST", headers=hdrs,
                json_body=payload, timeout=90)
    thinking_note = "thinking=disabled 已被接受"
    if c.status == 400 and "thinking" in c.text:
        payload.pop("thinking", None)
        c = request(f"{DS_BASE}/chat/completions", method="POST", headers=hdrs,
                    json_body=payload, timeout=90)
        thinking_note = "API 未接受 thinking 参数，已改用默认参数重测（M2 再定）"

    usage: dict = {}
    if c.status == 200:
        body = c.json() or {}
        usage = body.get("usage") or {}
        choices = body.get("choices") or [{}]
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        detail.append(f"POST /chat/completions → HTTP 200（{c.elapsed * 1000:.0f} ms）| 返回模型 {body.get('model')}")
        detail.append(f"输出：{content[:40]!r} | {thinking_note}")
        detail.append(
            "token：prompt={} completion={} total={}（缓存命中 prompt_cache_hit_tokens={}）".format(
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                usage.get("total_tokens"), usage.get("prompt_cache_hit_tokens", "n/a"))
        )
        ok = ok and int(usage.get("total_tokens") or 0) > 0
    else:
        detail.append(f"POST /chat/completions → HTTP {c.status}：{c.text[:300]}")
        ok = False

    return Result(
        "deepseek", "DeepSeek API 连通", ok, detail,
        {
            "deepseek_ms": round(c.elapsed * 1000),
            "deepseek_prompt_tokens": usage.get("prompt_tokens"),
            "deepseek_completion_tokens": usage.get("completion_tokens"),
            "deepseek_total_tokens": usage.get("total_tokens"),
        },
    )


# ---------------------------------------------------------------- 检查 3

def check_wecom() -> Result:
    url = (os.environ.get("WECOM_WEBHOOK_URL") or "").strip()
    register_secret(url)
    if not url:
        return Result("wecom", "企业微信 webhook 连通（errcode == 0 才算成功）", False,
                      ["缺少环境变量 WECOM_WEBHOOK_URL（应由 GitHub Secrets 注入）"])

    detail = [f"目标：{mask_url(url)}"]
    content = (
        "## M0 连通性测试 ✅\n"
        "> Actions 云端 runner 已直连企业微信 webhook\n"
        f"> 仓库：{os.environ.get('GITHUB_REPOSITORY', '-')} · run {os.environ.get('GITHUB_RUN_ID', '-')}\n"
        f"> UTC：{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n"
        "\n收到这条消息，说明第三项连通成立（判定依据：errcode == 0）。"
    )
    r = post_wecom(url, content)
    body = r.json()
    raw = r.text.strip()
    errcode = (body or {}).get("errcode") if isinstance(body, dict) else None

    detail.append(f"HTTP {r.status}（{r.elapsed * 1000:.0f} ms）| body={raw[:300]}")
    detail.append(
        f"errcode = {errcode!r} → 判定："
        f"{'成功（errcode == 0）' if errcode == 0 else '失败（errcode != 0 或无法解析）'}"
    )
    ok = r.status == 200 and errcode == 0
    if not ok:
        detail.append("推送未成功。注意：HTTP 200 但 errcode != 0 也算失败（策划书 7.1 / A-10）")

    return Result("wecom", "企业微信 webhook 连通（errcode == 0 才算成功）", ok, detail,
                  {"wecom_status": r.status, "wecom_errcode": errcode})


# ---------------------------------------------------------------- 检查 4

def check_wecom_bad_key() -> Result:
    real = (os.environ.get("WECOM_WEBHOOK_URL") or "").strip()
    if real:
        bad = replace_key(real, BAD_WECOM_KEY)
        source = "在真实 webhook 地址上把 key 换成固定无效值"
    else:
        bad = f"{WECOM_SEND}?key={BAD_WECOM_KEY}"
        source = "未配置真实 webhook，直接用同 endpoint + 固定无效 key"

    detail = [
        f"构造：{source}",
        f"目标：{mask_url(bad)}",
        "发一条预期不会进群的 markdown：不重试、不改 HTTP 状态码，只看 body",
    ]
    r = post_wecom(bad, "## 故障注入：错误 webhook key\n> 这条预期不会进群。")
    body = r.json()
    errcode = (body or {}).get("errcode") if isinstance(body, dict) else None
    detail.append(f"HTTP {r.status} | body={r.text.strip()[:300]}")
    ok = r.status == 200 and errcode == EXPECTED_BAD_ERRCODE
    detail.append(
        f"期望：HTTP 200 + errcode {EXPECTED_BAD_ERRCODE}；"
        f"实际：HTTP {r.status} + errcode {errcode} → {'一致（A-10 复现）' if ok else '不一致，形态变了'}"
    )
    if ok:
        detail.append("结论：只判 HTTP 状态码会把这次失败当成功，静默失败风险成立（策划书 7.1）")

    return Result("wecom_bad_key", "故障注入 A：错误 webhook key", ok, detail,
                  {"bad_wecom_status": r.status, "bad_wecom_errcode": errcode})


# ---------------------------------------------------------------- 检查 5

def check_deepseek_bad_key() -> Result:
    detail = ["使用固定无效 key（sk-000…000）调用 /chat/completions，验证失效形态"]
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    r = request(f"{DS_BASE}/chat/completions", method="POST",
                headers={"Authorization": f"Bearer {BAD_DS_KEY}"},
                json_body=payload, timeout=TIMEOUT)
    detail.append(f"HTTP {r.status}（{r.elapsed * 1000:.0f} ms）| body={r.text.strip()[:300]}")
    ok = r.status == 401
    detail.append(
        f"期望：HTTP 401 authentication_error（A-11）；实际：HTTP {r.status} → {'一致' if ok else '不一致，形态变了'}"
    )
    if ok:
        detail.append("结论：401 明确可判，重试策略必须对它短路（策划书 7.2）")

    return Result("deepseek_bad_key", "故障注入 B：错误 DeepSeek key", ok, detail,
                  {"bad_deepseek_status": r.status})


# ---------------------------------------------------------------- main

def run_check(fn, index: int, total: int) -> Result:
    header = f"检查 {fn.__name__}"
    title_guess = header
    started = time.monotonic()
    try:
        res = fn()
    except Exception as e:  # noqa: BLE001
        res = Result(fn.__name__, title_guess, False,
                     [f"检查过程异常：{type(e).__name__}: {e}"])
    say(f"\n[{index}/{total}] {res.title}")
    say("-" * 64)
    for line in res.detail:
        for sub in str(line).splitlines() or [""]:
            say("  " + sub)
    say(f"  → {'PASS ✅' if res.ok else 'FAIL ❌'}（{time.monotonic() - started:.1f}s）")
    return res


def main() -> int:
    register_secret(os.environ.get("DEEPSEEK_API_KEY"))
    register_secret(os.environ.get("WECOM_WEBHOOK_URL"))
    register_secret(os.environ.get("GITHUB_TOKEN"))

    say("=" * 64)
    say("M0 连通性冒烟测试 + 故障注入")
    say("=" * 64)
    say(f"python      : {sys.version.split()[0]} | {platform.platform()}")
    say(f"runner      : {os.environ.get('RUNNER_OS', '-')} / {os.environ.get('RUNNER_NAME', '-')}")
    say(f"in actions  : {os.environ.get('GITHUB_ACTIONS', 'false')}")
    say(f"repository  : {os.environ.get('GITHUB_REPOSITORY', '-')}")
    say(f"run         : {os.environ.get('GITHUB_RUN_ID', '-')} (attempt {os.environ.get('GITHUB_RUN_ATTEMPT', '-')})")
    say(f"commit      : {(os.environ.get('GITHUB_SHA') or '-')[:7]}")
    say(f"utc now     : {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
    say(f"model       : {MODEL}")

    checks = [check_github, check_deepseek, check_wecom,
              check_wecom_bad_key, check_deepseek_bad_key]
    results = [run_check(fn, i + 1, len(checks)) for i, fn in enumerate(checks)]

    say("\n" + "=" * 64)
    say("M0 汇总")
    say("=" * 64)
    for res in results:
        say(f"  {'✅' if res.ok else '❌'}  {res.title}")
    passed = sum(1 for r in results if r.ok)
    total = len(results)

    evidence: dict = {}
    for res in results:
        evidence.update(res.evidence)
    say("\nEVIDENCE（日志检索锚点）")
    for k, v in evidence.items():
        say(f"  {k} = {v}")

    ok = passed == total
    say(f"\nM0 RESULT: {'PASS' if ok else 'FAIL'} ({passed}/{total})")
    if not ok:
        say("失败项见上方详情；M0 不过不得进入 M1（策划书 8 节）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
