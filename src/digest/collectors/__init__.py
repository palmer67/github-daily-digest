"""采集器：统一接口 collect(client, column, today) -> list[Candidate]。"""

from __future__ import annotations

from .base import CollectorError

__all__ = ["CollectorError"]
