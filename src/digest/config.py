"""配置加载与 pydantic 校验（策划书第 4 节）。

硬性要求：缺字段、拼写错误、取值越界，必须在**启动时立刻**报清晰错误，
不许静默用默认值跑到一半才炸。所以：
  - 每层模型都 extra="forbid"，多写一个 key 也算错
  - ValidationError 统一翻译成「哪个字段 + 错在哪 + 应该怎么改」的中文
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .filters import Filters

SortKey = Literal["trending_order", "stars", "updated", "active_stars", "recent_activity"]
SourceKind = Literal["github_trending", "github_search", "github_topic"]


class ConfigError(RuntimeError):
    """配置错误。消息面向人，直接打印即可。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryCfg(_Strict):
    max: int = Field(3, ge=0, le=10)
    backoff: list[int] = Field(default_factory=lambda: [1, 4, 12])


class LLMCfg(_Strict):
    provider: Literal["deepseek"] = "deepseek"
    model: str = "deepseek-flash"
    thinking: Literal["disabled", "enabled"] = "disabled"
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    timeout_s: int = Field(90, ge=1, le=600)
    retry: RetryCfg = Field(default_factory=RetryCfg)


class GlobalCfg(_Strict):
    timezone: str = "Asia/Shanghai"
    dedupe_days: int = Field(7, ge=0, le=90)
    items_per_column: int = Field(5, ge=1, le=20)
    backfill: bool = True
    max_message_bytes: int = Field(4000, gt=0, le=4096)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    notifier: Literal["wecom", "serverchan"] = "wecom"


class FiltersCfg(_Strict):
    exclude_topics: list[str] = Field(default_factory=lambda: ["awesome-list", "awesome", "list"])
    exclude_name_patterns: list[str] = Field(
        default_factory=lambda: ["awesome", "-list$", "cheatsheet", "public-apis", "free-for-dev"]
    )
    min_readme_chars: int = Field(500, ge=0)
    require_description: bool = True
    require_language: bool = True
    max_star_fork_ratio: float = Field(20.0, gt=0)
    duplicate_similarity: float = Field(0.9, ge=0.0, le=1.0)


class ColumnCfg(_Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    enabled: bool = True
    source: SourceKind
    query: str | None = None
    since: Literal["daily", "weekly", "monthly"] | None = None
    sort: SortKey = "stars"
    window_days: int | None = Field(None, ge=1, le=365)
    dedupe_days: int | None = Field(None, ge=0, le=90)
    max_items: int | None = Field(None, ge=1, le=20)
    pool: int = Field(40, ge=5, le=100)
    exclude_keywords: list[str] = Field(default_factory=list)
    prompt_override: str | None = None

    @model_validator(mode="after")
    def _check_semantics(self) -> "ColumnCfg":
        if self.source in ("github_search", "github_topic") and not (self.query or "").strip():
            raise ValueError(f"栏目 {self.id}：source={self.source} 必须提供 query")
        if self.source == "github_trending" and (self.query or "").strip():
            raise ValueError(f"栏目 {self.id}：source=github_trending 不接受 query（榜单不需要查询串）")
        if self.sort == "trending_order" and self.source != "github_trending":
            raise ValueError(f"栏目 {self.id}：sort=trending_order 只对 github_trending 有意义")
        if self.source == "github_trending" and self.sort != "trending_order":
            raise ValueError(f"栏目 {self.id}：source=github_trending 的 sort 必须是 trending_order")
        return self


class Config(_Strict):
    version: int = Field(1, ge=1, le=1)
    global_cfg: GlobalCfg = Field(alias="global")
    filters: FiltersCfg = Field(default_factory=FiltersCfg)
    columns: list[ColumnCfg] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _check_columns(self) -> "Config":
        ids = [c.id for c in self.columns]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"栏目 id 重复：{', '.join(sorted(dup))}")
        if not any(c.enabled for c in self.columns):
            raise ValueError("至少要有 1 个 enabled: true 的栏目")
        return self

    # ---------- 便捷访问 ----------

    @property
    def enabled_columns(self) -> list[ColumnCfg]:
        return [c for c in self.columns if c.enabled]

    def items_per_column(self, column: ColumnCfg | None = None) -> int:
        if column is not None and column.max_items:
            return column.max_items
        return self.global_cfg.items_per_column

    def columns_by_id(self) -> dict[str, ColumnCfg]:
        return {c.id: c for c in self.columns}

    def filters_for(self, column: ColumnCfg) -> Filters:
        """全局过滤规则 + 栏目级黑名单。"""
        f = self.filters
        return Filters(
            exclude_topics=tuple(f.exclude_topics),
            exclude_name_patterns=tuple(f.exclude_name_patterns),
            min_readme_chars=f.min_readme_chars,
            require_description=f.require_description,
            require_language=f.require_language,
            max_star_fork_ratio=f.max_star_fork_ratio,
            exclude_keywords=tuple(column.exclude_keywords),
            duplicate_similarity=f.duplicate_similarity,
        )


# ---------- 错误翻译 ----------

_TYPE_TEXT: dict[str, str] = {
    "missing": "缺少必填项",
    "extra_forbidden": "未知配置项（可能拼错了）",
    "literal_error": "取值不在允许范围内",
    "int_parsing": "应为整数",
    "int_type": "应为整数",
    "float_parsing": "应为数字",
    "string_type": "应为字符串",
    "bool_parsing": "应为 true/false",
    "list_type": "应为列表",
    "greater_than": "数值太小",
    "greater_than_equal": "数值太小",
    "less_than": "数值太大",
    "less_than_equal": "数值太大",
    "value_error": "字段之间有冲突",
}


def _fmt_path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "(根)"


def _friendly(error: dict[str, Any]) -> str:
    loc = tuple(error.get("loc", ()))
    etype = str(error.get("type", ""))
    msg = str(error.get("msg", "")).replace("Value error, ", "")
    where = _fmt_path(loc)

    if etype == "extra_forbidden":
        parent = _fmt_path(loc[:-1])
        allowed = {
            "": "version, global, filters, columns",
            "global": (
                "timezone, dedupe_days, items_per_column, backfill, "
                "max_message_bytes, llm, notifier"
            ),
            "global.llm": "provider, model, thinking, temperature, timeout_s, retry",
            "filters": (
                "exclude_topics, exclude_name_patterns, min_readme_chars, "
                "require_description, require_language, max_star_fork_ratio, "
                "duplicate_similarity"
            ),
        }.get(parent)
        hint = f"；{parent or '顶层'} 允许的项：{allowed}" if allowed else ""
        return f"{where}：{_TYPE_TEXT[etype]}（{msg}）{hint}"

    label = _TYPE_TEXT.get(etype)
    if etype == "literal_error" or etype == "value_error":
        return f"{where}：{label}——{msg}"
    if label:
        return f"{where}：{label}——{msg}"
    return f"{where}：{msg}"


def load_config(path: str | Path) -> Config:
    """读 YAML 并校验。任何问题都抛 ConfigError，消息可直接打印给用户看。"""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"找不到配置文件：{p}")
    raw = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列：" if mark else ""
        raise ConfigError(f"YAML 语法错误（{p}）{where}{getattr(e, 'problem', e)}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射（key: value），实际是 {type(data).__name__}")

    try:
        return Config.model_validate(data)
    except ValidationError as e:
        lines = [_friendly(err) for err in e.errors()]
        raise ConfigError(
            f"配置校验失败（{p}），共 {len(lines)} 处：\n"
            + "\n".join(f"  - {line}" for line in lines)
        ) from e
