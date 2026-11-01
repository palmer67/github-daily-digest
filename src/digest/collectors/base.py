"""采集器公共部分。"""

from __future__ import annotations


class CollectorError(RuntimeError):
    """采集失败。单栏目失败不应拖垮整批（7.4）。"""
