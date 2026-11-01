"""今日热门：解析 github.com/trending 页面（无官方 API，方案 a，见 A-3）。

页面结构（2026-11-01 实测）：
  <article class="Box-row">
    <h2 ...><a href="/owner/repo">
    <p class="col-9 color-fg-muted ...">简介</p>
    <span itemprop="programmingLanguage">语言</span>
    1,234 stars today
"""

from __future__ import annotations

import re

from ..config import ColumnCfg
from ..github import GitHubClient
from ..models import Candidate
from .base import CollectorError

ARTICLE_RE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
NAME_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"')
DESC_RE = re.compile(r'<p class="col-9 color-fg-muted[^"]*">\s*(.*?)\s*</p>', re.S)
LANG_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
TODAY_RE = re.compile(r"([\d,]+)\s*stars today")
TAG_RE = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return TAG_RE.sub("", html).replace("&amp;", "&").replace("&#39;", "'").strip()


def collect(client: GitHubClient, column: ColumnCfg, today=None) -> list[Candidate]:
    since = column.since or "daily"
    html = client.get_text(f"https://github.com/trending?since={since}")

    blocks = ARTICLE_RE.findall(html)
    out: list[Candidate] = []
    for block in blocks:
        m = NAME_RE.search(block)
        if not m:
            continue
        full_name = m.group(1).strip().strip("/")
        if full_name.count("/") != 1:
            continue
        desc = DESC_RE.search(block)
        lang = LANG_RE.search(block)
        today_m = TODAY_RE.search(block)
        out.append(
            Candidate(
                full_name=full_name,
                source="github_trending",
                description=_text(desc.group(1)) if desc else "",
                language=lang.group(1).strip() if lang else None,
                stars_today=int(today_m.group(1).replace(",", "")) if today_m else None,
            )
        )

    if not out:
        # 策划书第 10 节：页面结构变化是一期最大单点风险，解析到 0 条必须炸，不许静默推空栏目
        raise CollectorError(
            "Trending 解析到 0 条：页面结构可能变了（A-3 记录的 <article class=\"Box-row\"> 没匹配上）"
        )
    return out
