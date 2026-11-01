"""GitHub HTTP 客户端（只用标准库）。

按 7.2 的错误码表：
  - 401/404 这类明确错误：立刻抛，不浪费重试次数
  - 429 / 5xx / 网络超时：重试 3 次，退避 1/4/12 秒
  - 403 且限流打满：立刻抛 RateLimited，让上层走 raw 兜底或跳过该栏目
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API = "https://api.github.com"
USER_AGENT = "github-daily-digest/0.1 (+https://github.com/palmer67/github-daily-digest)"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    """403 + 配额打满。上层应换 raw 兜底或跳过本栏目。"""


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        timeout: float = 30.0,
        backoff: tuple[float, ...] = (1, 4, 12),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = (token or os.environ.get("GITHUB_TOKEN", "")).strip() or None
        self.timeout = timeout
        self.backoff = backoff
        self.sleep = sleep
        self.calls = 0

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self, accept: str | None = None) -> dict[str, str]:
        h = {
            "User-Agent": USER_AGENT,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, url: str, accept: str | None = None) -> tuple[int, bytes, dict[str, str]]:
        attempt = 0
        while True:
            self.calls += 1
            req = urllib.request.Request(url, headers=self._headers(accept))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read(), dict(resp.headers)
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()[:400]
                except Exception:  # noqa: BLE001 - 读错误体失败不应掩盖原始状态码
                    pass
                status = e.code
                remaining = (e.headers or {}).get("X-RateLimit-Remaining")
                text = body.decode("utf-8", "replace")
                if status == 403 and (remaining == "0" or "rate limit" in text.lower()):
                    raise RateLimited(
                        f"GitHub 403 限流打满（剩余 {remaining}）：{url}", status=403
                    ) from e
                if status in (429, 500, 502, 503, 504) and attempt < len(self.backoff):
                    self.sleep(self.backoff[attempt])
                    attempt += 1
                    continue
                raise GitHubError(f"GitHub {status} {url}：{text}", status=status) from e
            except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
                if attempt < len(self.backoff):
                    self.sleep(self.backoff[attempt])
                    attempt += 1
                    continue
                raise GitHubError(f"网络错误（重试 {attempt} 次后放弃）{url}：{e}") from e

    def get_json(self, url: str, accept: str | None = None) -> Any:
        _, body, _ = self._request(url, accept=accept)
        return json.loads(body.decode("utf-8"))

    def get_text(self, url: str, accept: str | None = None) -> str:
        _, body, _ = self._request(url, accept=accept)
        return body.decode("utf-8", "replace")

    # ---------- 业务接口 ----------

    def rate_limit(self) -> dict[str, Any]:
        return self.get_json(f"{API}/rate_limit")

    def search_repositories(
        self, query: str, sort: str = "stars", order: str = "desc", per_page: int = 40
    ) -> dict[str, Any]:
        qs = urllib.parse.urlencode(
            {"q": query, "sort": sort, "order": order, "per_page": str(per_page)}
        )
        return self.get_json(f"{API}/search/repositories?{qs}")

    def get_repo(self, full_name: str) -> dict[str, Any]:
        return self.get_json(f"{API}/repos/{full_name}")
