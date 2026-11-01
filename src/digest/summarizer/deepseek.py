"""DeepSeek 调用（OpenAI 兼容），错误分流按 7.2 的表。

关键决定（策划书 7.2 的批注）：**401/402 绝不重试**。
v1.0 写的是「失败重试 3 次」，对 key 失效这种确定性错误，25 个项目各重试 3 次
= 75 次无效请求 + 每次退避等待，任务会直接撞上 Actions 超时，还把真正的原因埋在日志里。
所以这里把错误分成两类：确定性（认证/余额/参数）立刻抛，临时性（429/5xx/超时）才退避重试。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT = 90
DEFAULT_BACKOFF = (1, 4, 12)

# 价格（美元/百万 token，2026-11 官方 off-peak，A-13），只用来估算花费
PRICE_INPUT_MISS = 0.15
PRICE_INPUT_HIT = 0.003
PRICE_OUTPUT = 0.60


class LLMError(Exception):
    """LLM 相关错误的基类。"""


class LLMAuthError(LLMError):
    """401 / 402 / 403 —— 重试没用，必须立刻降级 + 告警。"""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"DeepSeek 认证/余额问题 HTTP {status}: {detail[:200]}")


class LLMTransientError(LLMError):
    """429 / 5xx / 超时 / 网络错误 —— 可以退避重试。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class LLMFormatError(LLMError):
    """200 但不是合法 JSON 对象 —— 单卡重问一次。"""


def _default_transport(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    for attempt in (1, 2):  # SSL 抖动重开一次，不计入业务重试
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, socket.timeout) as e:
            if attempt == 2:
                raise LLMTransientError(f"网络错误：{e}") from e
    raise LLMTransientError("网络错误：重试后仍失败")


class DeepSeekClient:
    """最小可用的 DeepSeek 客户端。transport 可注入，方便离线单测。"""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 3,
        backoff: tuple[int, ...] = DEFAULT_BACKOFF,
        temperature: float = 0.2,
        max_tokens: int = 800,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[[str, dict[str, str], bytes, float], tuple[int, str]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.sleep = sleep
        self.transport = transport or _default_transport
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

    # ---------------------------------------------------------------- 内部
    def _post(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            # 实测：关掉 thinking 后同样的卡片输出更快、更稳（5.4）
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/chat/completions"

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                status, text = self.transport(url, headers, body, self.timeout)
            except LLMTransientError as e:
                last_error = e
            else:
                if status == 200:
                    return self._parse(text)
                detail = _error_detail(text)
                if status in (401, 402, 403):
                    raise LLMAuthError(status, detail)
                if status == 429 or 500 <= status < 600:
                    last_error = LLMTransientError(f"HTTP {status}: {detail[:150]}")
                else:
                    # 400 之类属于请求本身有问题，重试也是同样的结果
                    raise LLMError(f"DeepSeek 请求被拒 HTTP {status}: {detail[:200]}")
            if attempt < self.retries:
                self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        raise LLMTransientError(f"重试 {self.retries} 次仍失败：{last_error}")

    def _parse(self, text: str) -> dict[str, Any]:
        raw = text.strip()
        if raw.startswith("```"):  # 个别情况下会有代码块围栏
            raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
            raw = raw.removeprefix("json").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMFormatError(f"返回的不是 JSON：{e}；前 120 字符：{raw[:120]!r}") from e
        if not isinstance(data, dict):
            raise LLMFormatError(f"返回的 JSON 不是对象：{type(data).__name__}")
        usage = data.pop("usage", None) or {}
        if isinstance(usage, dict):
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details") or {}
            if isinstance(details, dict):
                self.usage["cached_tokens"] += int(details.get("cached_tokens") or 0)
        # 有些实现把内容塞在 choices 里；这里只接受直接的 JSON 对象
        if "choices" in data and "what" not in data:
            try:
                content = data["choices"][0]["message"]["content"]
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise LLMFormatError("choices 里的内容不是 JSON 对象")
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                raise LLMFormatError(f"choices 解析失败：{e}") from e
        return data

    # ---------------------------------------------------------------- 对外
    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """一次问答，返回解析好的 JSON 对象。错误按 7.2 分流。"""
        return self._post(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def cost_usd(self) -> float:
        """粗略花费估算（A-12 的算法）。"""
        cached = self.usage["cached_tokens"]
        prompt = max(0, self.usage["prompt_tokens"] - cached)
        return (
            prompt / 1_000_000 * PRICE_INPUT_MISS
            + cached / 1_000_000 * PRICE_INPUT_HIT
            + self.usage["completion_tokens"] / 1_000_000 * PRICE_OUTPUT
        )


def _error_detail(text: str) -> str:
    try:
        data = json.loads(text)
        err = data.get("error") or {}
        if isinstance(err, dict):
            return f"{err.get('type') or ''} {err.get('message') or ''}".strip() or str(data)[:200]
        return str(data)[:200]
    except json.JSONDecodeError:
        return text.strip()[:200] or "（空响应体）"
