"""搜索类采集：github_search / github_topic。

查询串里的 {14d}/{30d} 会替换成「今天往前推 N 天」的日期，配置里不写死日期。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ..config import ColumnCfg
from ..github import GitHubClient
from ..models import Candidate
from .base import CollectorError

DATE_PLACEHOLDER = re.compile(r"\{(\d+)d\}")

# 栏目排序 → GitHub search 的 sort 参数（本地还会再精排一次）
SEARCH_SORT = {
    "stars": "stars",
    "updated": "updated",
    "active_stars": "stars",
    "recent_activity": "updated",
    "trending_order": "stars",
}


def expand_query(query: str, today: date) -> str:
    return DATE_PLACEHOLDER.sub(lambda m: (today - timedelta(days=int(m.group(1)))).isoformat(), query)


def to_candidate(item: dict, source: str) -> Candidate:
    return Candidate(
        full_name=item.get("full_name", ""),
        source=source,
        description=(item.get("description") or "").strip(),
        language=item.get("language"),
        stars=int(item.get("stargazers_count") or 0),
        forks=int(item.get("forks_count") or 0),
        topics=list(item.get("topics") or []),
        pushed_at=item.get("pushed_at"),
        created_at=item.get("created_at"),
        html_url=item.get("html_url", ""),
    )


def collect(client: GitHubClient, column: ColumnCfg, today: date) -> list[Candidate]:
    assert column.query, f"栏目 {column.id} 缺少 query"
    query = expand_query(column.query, today)
    if column.source == "github_topic" and not query.lower().startswith("topic:"):
        query = f"topic:{query}"

    sort = SEARCH_SORT.get(column.sort, "stars")
    data = client.search_repositories(query, sort=sort, per_page=column.pool)
    items = data.get("items") or []
    if not items:
        raise CollectorError(f"栏目 {column.id} 搜索返回 0 条：query={query!r}")
    return [to_candidate(it, column.source) for it in items if it.get("full_name")]
