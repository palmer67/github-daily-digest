"""README 抓取（5.1 第 6 步、6.2 配额约束）。

顺序：
  1. raw.githubusercontent.com —— 实测可用且**完全不消耗 API 配额**（A-5），优先
  2. API /repos/{full}/readme —— 仅作回退

这里只负责「拿到原文 + 字符数」，分段裁剪（3.4 的头部 3000 + 安装段 3000）属于 M2 的提示词组装。
"""

from __future__ import annotations

from .github import GitHubClient, GitHubError

RAW = "https://raw.githubusercontent.com"
BRANCHES = ("main", "master", "dev")
FILENAMES = ("README.md", "readme.md", "README.rst", "README", "README.markdown", "README.txt")


def fetch_readme(client: GitHubClient, full_name: str) -> tuple[str | None, str]:
    """返回 (README 原文, 来源标注)。拿不到返回 (None, 'missing')。"""
    for branch in BRANCHES:
        for name in FILENAMES:
            url = f"{RAW}/{full_name}/{branch}/{name}"
            try:
                return client.get_text(url), f"raw:{branch}/{name}"
            except GitHubError as e:
                if e.status in (404, 400):
                    continue
                # 429/5xx 已经在客户端内部重试过，这里继续换下一个文件名只会更慢
                break
    try:
        text = client.get_text(
            f"https://api.github.com/repos/{full_name}/readme",
            accept="application/vnd.github.raw",
        )
        return text, "api"
    except GitHubError:
        return None, "missing"
