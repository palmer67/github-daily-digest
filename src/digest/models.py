"""采集层统一数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Candidate:
    """一个候选项目。字段刻意保持扁平，方便直接落 JSON 给 M2 消费。"""

    full_name: str
    source: str
    description: str = ""
    language: str | None = None
    stars: int = 0
    forks: int = 0
    topics: list[str] = field(default_factory=list)
    pushed_at: str | None = None
    created_at: str | None = None
    stars_today: int | None = None
    readme_chars: int | None = None
    html_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.html_url or f"https://github.com/{self.full_name}"

    def star_fork_ratio(self) -> float | None:
        """star/fork 比。fork 为 0 时返回 None（不能除零，也不该判成通胀）。"""
        if self.forks <= 0:
            return None
        return self.stars / self.forks

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        d["url"] = self.url
        ratio = self.star_fork_ratio()
        d["star_fork_ratio"] = round(ratio, 2) if ratio is not None else None
        return d
