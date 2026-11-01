"""3.5 内容质量过滤。

全部是纯函数：输入 Candidate，输出「丢弃原因」或 None。不碰网络、不碰文件，
所以每条规则都能单独写单测（M1 验收要求）。

丢弃原因用机器可读的短字符串，日志和 JSON 里直接用，方便统计「今天被什么规则扔了多少」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Candidate

# 丢弃原因（同时也是 stats 里的键名）
REASON_NO_DESCRIPTION = "no_description"
REASON_NO_LANGUAGE = "no_language"
REASON_AWESOME_NAME = "awesome_name"
REASON_AWESOME_TOPIC = "awesome_topic"
REASON_STAR_FORK_RATIO = "star_fork_ratio"
REASON_README_TOO_SHORT = "readme_too_short"
REASON_KEYWORD_BLACKLIST = "keyword_blacklist"
REASON_STALE = "stale"

# 人类可读的原因说明，给日志用（策划书 9.4：错误要清楚）
REASON_TEXT = {
    REASON_NO_DESCRIPTION: "没有简介",
    REASON_NO_LANGUAGE: "没有主语言",
    REASON_AWESOME_NAME: "清单仓库（名字命中 awesome/-list/cheatsheet 等）",
    REASON_AWESOME_TOPIC: "清单仓库（topic 命中 awesome/list）",
    REASON_STAR_FORK_RATIO: "star/fork 比过高，疑似 star 通胀",
    REASON_README_TOO_SHORT: "README 太短（<500 字符）",
    REASON_KEYWORD_BLACKLIST: "命中合规黑名单",
    REASON_STALE: "窗口期内没有活跃（pushed_at 太旧）",
}


@dataclass
class Filters:
    """从 config.filters 构造。字段名与 YAML 一一对应。"""

    exclude_topics: tuple[str, ...] = ("awesome-list", "awesome", "list")
    exclude_name_patterns: tuple[str, ...] = (
        "awesome",
        "-list$",
        "cheatsheet",
        "public-apis",
        "free-for-dev",
    )
    min_readme_chars: int = 500
    require_description: bool = True
    require_language: bool = True
    max_star_fork_ratio: float = 20.0
    exclude_keywords: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        patterns = [p for p in self.exclude_name_patterns if p]
        self._name_re = re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None

    # ---------- 三道关卡 ----------

    def check_metadata(self, c: Candidate) -> str | None:
        """便宜检查：只用搜索/榜单页自带字段，不花任何额外请求。"""
        if self.require_description and not (c.description or "").strip():
            return REASON_NO_DESCRIPTION
        if self.require_language and not (c.language or "").strip():
            return REASON_NO_LANGUAGE
        if self._name_re and self._name_re.search(c.full_name):
            return REASON_AWESOME_NAME
        if self.exclude_keywords:
            blob = f"{c.full_name} {c.description}".lower()
            for kw in self.exclude_keywords:
                if kw and kw.lower() in blob:
                    return REASON_KEYWORD_BLACKLIST
        ratio = c.star_fork_ratio()
        if ratio is not None and ratio > self.max_star_fork_ratio:
            return REASON_STAR_FORK_RATIO
        return None

    def check_enriched(self, c: Candidate) -> str | None:
        """补充检查：用 /repos 拿到的 topics（搜索结果本身不带 topics）。"""
        topics = {t.lower() for t in (c.topics or [])}
        for t in self.exclude_topics:
            if t.lower() in topics:
                return REASON_AWESOME_TOPIC
        return None

    def check_readme(self, c: Candidate) -> str | None:
        """README 长度检查：必须已经拉过 README（c.readme_chars 有值）。"""
        if c.readme_chars is None:
            return None  # 没拉到不算不合格，交给上层记 readme_missing
        if c.readme_chars < self.min_readme_chars:
            return REASON_README_TOO_SHORT
        return None


def describe_reason(reason: str) -> str:
    return REASON_TEXT.get(reason, reason)
