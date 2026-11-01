"""单个栏目的采集流水线（5.1 第 4–6 步）。

顺序刻意设计成「先便宜后昂贵」：
  1. 抓原始候选（榜单页 / 搜索 API，一次请求）
  2. 便宜过滤：只用榜单/搜索自带的字段（简介、语言、名字、star/fork、黑名单）
  3. 按栏目排序规则排好
  4. 对排在前面的候选逐个「贵操作」：/repos 补 topics → 拉 README → 长度检查
  5. 去重 + 补位，得到最终 N 条
每一步丢掉的候选都记进 stats，日志里能说清「今天被什么规则扔了多少」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .collectors import search, trending
from .collectors.base import CollectorError
from .config import ColumnCfg, Config
from .filters import REASON_STALE, describe_reason
from .github import GitHubClient, GitHubError
from .models import Candidate
from .readme import fetch_readme
from .state import apply_dedupe, backfill, parse_date


@dataclass
class ColumnResult:
    column: ColumnCfg
    items: list[Candidate] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def shortfall(self) -> int:
        return int(self.stats.get("shortfall", 0))


def _bump(stats: dict[str, Any], reason: str) -> None:
    dropped = stats["dropped"]
    dropped[reason] = dropped.get(reason, 0) + 1


def sort_candidates(items: list[Candidate], column: ColumnCfg) -> list[Candidate]:
    """栏目排序规则（4 节 config 里的 sort 字段）。"""
    if column.sort == "trending_order":
        return items
    if column.sort == "stars":
        return sorted(items, key=lambda c: (-c.stars, c.full_name.lower()))
    if column.sort == "active_stars":
        return sorted(items, key=lambda c: (-c.stars, c.full_name.lower()))
    if column.sort == "updated":
        return sorted(items, key=lambda c: (c.pushed_at or "", c.full_name.lower()), reverse=True)
    if column.sort == "recent_activity":
        return sorted(items, key=lambda c: (c.pushed_at or "", c.full_name.lower()), reverse=True)
    return items


def within_window(c: Candidate, column: ColumnCfg, today: date) -> bool:
    """window_days 内的活跃度门槛（羊毛专区按近 30 天活跃排，不按总 star）。"""
    if not column.window_days:
        return True
    pushed = parse_date(c.pushed_at)
    if pushed is None:
        return False
    return (today - pushed).days <= column.window_days


def enrich(client: GitHubClient, c: Candidate) -> None:
    """用 /repos/{full} 校正元数据并补上 topics（搜索结果本身不带 topics）。"""
    meta = client.get_repo(c.full_name)
    c.stars = int(meta.get("stargazers_count") or c.stars or 0)
    c.forks = int(meta.get("forks_count") or c.forks or 0)
    c.language = meta.get("language") or c.language
    c.description = (meta.get("description") or c.description or "").strip()
    c.topics = list(meta.get("topics") or c.topics or [])
    c.pushed_at = meta.get("pushed_at") or c.pushed_at
    c.created_at = meta.get("created_at") or c.created_at
    c.html_url = meta.get("html_url") or c.html_url


def collect_raw(client: GitHubClient, column: ColumnCfg, today: date) -> list[Candidate]:
    if column.source == "github_trending":
        return trending.collect(client, column, today)
    return search.collect(client, column, today)


def run_column(
    client: GitHubClient,
    config: Config,
    column: ColumnCfg,
    sent: dict[str, str],
    today: date,
    log: Callable[[str], None] = print,
) -> ColumnResult:
    need = config.items_per_column(column)
    filters = config.filters_for(column)
    stats: dict[str, Any] = {
        "raw": 0,
        "qualified": 0,
        "kept": 0,
        "deduped": 0,
        "backfilled": 0,
        "shortfall": 0,
        "enrich_failed": 0,
        "readme_missing": 0,
        "dropped": {},
        "readme_source": {"raw": 0, "api": 0, "missing": 0},
    }
    result = ColumnResult(column=column, stats=stats)

    try:
        raw = collect_raw(client, column, today)
    except (CollectorError, GitHubError) as e:
        result.error = str(e)
        stats["error"] = str(e)
        return result

    stats["raw"] = len(raw)

    # 第 2 步：便宜过滤
    ordered: list[Candidate] = []
    for c in raw:
        if not within_window(c, column, today):
            _bump(stats, REASON_STALE)
            continue
        reason = filters.check_metadata(c)
        if reason:
            _bump(stats, reason)
            continue
        ordered.append(c)
    ordered = sort_candidates(ordered, column)

    # 第 4 步：贵操作。多攒 need 条做补位储备（7.3 的「取第 6、7、8 名」）
    target = need * 2
    qualified: list[Candidate] = []
    for c in ordered:
        if len(qualified) >= target:
            break
        try:
            enrich(client, c)
        except GitHubError as e:
            stats["enrich_failed"] += 1
            log(f"      ! {c.full_name} 元数据补全失败，用榜单/搜索自带数据继续：{e}")
        reason = filters.check_enriched(c)
        if reason:
            _bump(stats, reason)
            continue

        text, how = fetch_readme(client, c.full_name)
        key = "raw" if how.startswith("raw") else how
        stats["readme_source"][key if key in stats["readme_source"] else "missing"] += 1
        if text is None:
            stats["readme_missing"] += 1
        else:
            c.readme_chars = len(text)
        reason = filters.check_readme(c)
        if reason:
            _bump(stats, reason)
            continue
        qualified.append(c)

    stats["qualified"] = len(qualified)

    # 第 5 步：去重 + 补位
    days = column.dedupe_days if column.dedupe_days is not None else config.global_cfg.dedupe_days
    fresh, deduped = apply_dedupe(qualified, sent, today, days)
    stats["deduped"] = len(deduped)

    if config.global_cfg.backfill:
        kept, shortfall = backfill(fresh[:need], fresh[need:], need)
    else:
        kept, shortfall = fresh[:need], max(0, need - len(fresh[:need]))

    stats["kept"] = len(kept)
    stats["backfilled"] = max(0, len(kept) - min(len(fresh[:need]), need))
    stats["shortfall"] = shortfall
    result.items = kept
    return result


def drop_summary(stats: dict[str, Any]) -> str:
    dropped: dict[str, int] = stats.get("dropped") or {}
    if not dropped:
        return "无"
    parts = [f"{describe_reason(k)} {v} 条" for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])]
    return " / ".join(parts)
