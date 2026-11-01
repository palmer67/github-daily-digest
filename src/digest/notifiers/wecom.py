"""推送抽象层（M3 只有企业微信）。

策划书 7.1 的最高优先级：**企业微信返回 HTTP 200 也可能是失败**。
所以这里的 `send_markdown()` 不返回「HTTP 状态码」，只返回解析后的 body；
`errcode != 0` 一律抛 `WeComError`，绝不把失败当成功。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

WECOM_HOST = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="

# 7.2 错误码处理表
RETRY_AFTER_SECONDS: dict[int, float] = {
    45009: 60.0,  # 企业微信频率超限：退避 60 秒后重试
    -1: 1.0,  # 系统繁忙
}
FATAL_ERRCODES: dict[int, str] = {
    93000: "webhook 失效（invalid webhook url / key 不对）",
    93001: "webhook 已停用",
    40058: "markdown 超过 4096 字节（拆分逻辑本该拦住）",
    40008: "消息类型不合法",
}

Transport = Callable[[str, bytes, float], tuple[int, str]]


def default_transport(url: str, payload: bytes, timeout: float) -> tuple[int, str]:
    """标准库实现：POST JSON，返回 (HTTP 状态码, body 文本)。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要读 body
        return e.code, e.read().decode("utf-8", "replace")


class NotifierError(Exception):
    """推送失败（消息里已经过掩码，可以安全写日志）。"""


class WeComError(NotifierError):
    def __init__(self, errcode: int, errmsg: str, hint: str = "") -> None:
        self.errcode = errcode
        self.errmsg = errmsg
        self.hint = hint
        detail = f"errcode={errcode} errmsg={errmsg}"
        if hint:
            detail += f" hint={hint}"
        super().__init__(detail)


def mask_url(url: str) -> str:
    """日志里只露出 key 的前 4 位和长度。"""
    key = url.split("key=", 1)[-1] if "key=" in url else url
    if len(key) <= 8:
        return f"{WECOM_HOST}***"
    return f"{WECOM_HOST}{key[:4]}***（{len(key)} 字符）"


class WeComNotifier:
    def __init__(
        self,
        webhook_url: str,
        timeout: float = 15.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        transport: Transport = default_transport,
    ) -> None:
        if not webhook_url:
            raise NotifierError("WECOM_WEBHOOK_URL 为空：企业微信 webhook 没配")
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep
        self._transport = transport
        self.sent = 0

    @property
    def masked(self) -> str:
        return mask_url(self.webhook_url)

    def send_markdown(self, content: str) -> dict[str, Any]:
        """发一条 markdown。成功返回 body（errcode == 0），失败抛 WeComError。"""
        payload = json.dumps(
            {"msgtype": "markdown", "markdown": {"content": content}},
            ensure_ascii=False,
        ).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                status, body = self._transport(self.webhook_url, payload, self.timeout)
            except Exception as e:  # 网络层错误：按可重试处理
                last_error = e
                if attempt < self.retries:
                    self._sleep(1.0 * (attempt + 1))
                    continue
                raise NotifierError(f"网络错误（{type(e).__name__}: {e}）") from e

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                raise NotifierError(
                    f"HTTP {status}，但 body 不是 JSON：{body[:120]!r}"
                ) from None

            errcode = int(data.get("errcode", -999))
            if errcode == 0:
                self.sent += 1
                return data

            errmsg = str(data.get("errmsg", ""))
            if errcode in FATAL_ERRCODES:
                raise WeComError(errcode, errmsg, FATAL_ERRCODES[errcode])

            wait = RETRY_AFTER_SECONDS.get(errcode)
            if wait is not None and attempt < self.retries:
                self._sleep(wait)
                last_error = WeComError(errcode, errmsg)
                continue
            raise WeComError(errcode, errmsg)

        raise NotifierError(f"重试 {self.retries} 次仍失败：{last_error}")
