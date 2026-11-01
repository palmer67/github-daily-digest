"""3.5 内容质量过滤。

全部是纯函数：输入 Candidate，输出「丢弃原因」或 None。不碰网络、不碰文件，
所以每条规则都能单独写单测（M1 验收要求）。

丢弃原因用机器可读的短字符串，日志和 JSON 里直接用，方便统计「今天被什么规则扔了多少」。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Sequence

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
REASON_DUPLICATE = "duplicate"

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
    REASON_DUPLICATE: "同批近似重复（疑似同一批刷量仓库）",
}

# 比较相似度前先把简介归一化：只留字母数字和汉字，避免标点/大小写制造假差异
_NORM_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


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
    # 同批近似重复：0 表示关闭。阈值 0.9 = 简介归一化后几乎逐字相同才算
    duplicate_similarity: float = 0.9
    duplicate_min_chars: int = 40

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

    def dedupe_similar(self, items: Sequence[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
        """同批近似重复只留第一条。

        为什么需要：搜索类栏目会碰到批量注册的刷量仓库——账号名不同、
        简介和 README 逐字复制，单看任何一条都"合格"，一起推出去就是垃圾。
        比较用简介（仓库最本质的一句话），归一化后看相似度；短简介不比较，避免误杀。
        顺序保持不变，第一条留下。
        """
        if self.duplicate_similarity <= 0:
            return list(items), []

        kept: list[Candidate] = []
        dropped: list[Candidate] = []
        seen: list[str] = []
        for item in items:
            text = normalize_description(item.description)
            if len(text) >= self.duplicate_min_chars and any(
                is_similar(text, prev, self.duplicate_similarity, self.duplicate_min_chars) for prev in seen
            ):
                dropped.append(item)
                continue
            kept.append(item)
            seen.append(text)
        return kept, dropped


def normalize_description(text: str | None) -> str:
    """小写 + 只留字母数字汉字：`AI API gateway` 与 `AI-API-Gateway!` 归一化后相同。"""
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def is_similar(a: str, b: str, threshold: float, min_chars: int) -> bool:
    """两段已归一化文本是否近似。共同前缀够长直接判同，省掉逐字符比较。"""
    if not a or not b:
        return False
    if len(a) < min_chars or len(b) < min_chars:
        return False
    if a[:min_chars] == b[:min_chars]:
        return True
    return difflib.SequenceMatcher(None, a[:300], b[:300]).ratio() >= threshold


def describe_reason(reason: str) -> str:
    return REASON_TEXT.get(reason, reason)
