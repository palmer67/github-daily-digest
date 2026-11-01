"""README 抓取（5.1 第 6 步、6.2 配额约束）。

顺序：
  1. raw.githubusercontent.com —— 实测可用且**完全不消耗 API 配额**（A-5），优先
  2. API /repos/{full}/readme —— 仅作回退

这里只负责「拿到原文 + 字符数」，分段裁剪（3.4 的头部 3000 + 安装段 3000）属于 M2 的提示词组装。
"""

from __future__ import annotations

import re

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

# ---------------------------------------------------------------- 3.4 分段裁剪
# 事故背景（策划书 3.4）：v1.0 只喂前 6000 字符，很多项目的安装说明、硬件要求在
# 中后段，直接被截没了，模型只好写「未说明」。所以改成头 3000 + 安装段 3000。

SECTION_RE = re.compile(
    r"(?im)^#{1,6}[^\n]*?(install|quick ?start|getting started|requirement|prerequisite|docker|hardware)"
)
HEAD_CHARS = 3000
SECTION_CHARS = 3000
ELISION = "\n\n…（中间省略）…\n\n"


def trim_for_prompt(text: str, head: int = HEAD_CHARS, section: int = SECTION_CHARS) -> tuple[str, str]:
    """把 README 裁成「头部 + 安装/要求段」，返回 (裁剪后文本, 裁剪说明)。"""
    if not text:
        return "", "empty"
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= head:
        return text, f"full:{len(text)}"

    head_text = text[:head]
    m = SECTION_RE.search(text)
    if not m:
        return head_text, f"head:{head}/{len(text)}"

    start = m.start()
    tail_text = text[start : start + section]
    if start < head:  # 安装段就在头部里，不用再拼
        return text[: max(head, start + section)], f"head:{head}/{len(text)}"
    note = f"head+section:{head}+{len(tail_text)}/{len(text)}"
    return head_text + ELISION + tail_text, note
