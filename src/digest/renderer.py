"""3.6 消息拆分（M1 只需要切分这一个纯函数，卡片渲染在 M3）。

实测：企业微信 markdown 精确 4096 字节（A-7），按 4000 字节切，留 96 字节给「续」标题。
切分必须在卡片边界 `\\n### ` 上切，绝不能把一张卡劈成两半。
"""

from __future__ import annotations

import re

CARD_SEP = "\n### "


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def split_markdown(text: str, limit_bytes: int = 4000) -> list[str]:
    """按卡片边界把 markdown 切成若干条，每条 ≤ limit_bytes。

    特殊情况：如果单张卡本身超过上限，它会被单独切成一块并原样返回
    （长度可能超限）——这种情况必须由调用方记录告警，不能静默截断。
    """
    if nbytes(text) <= limit_bytes:
        return [text]

    parts = [p for p in re.split(f"(?={re.escape(CARD_SEP)})", text) if p]
    chunks: list[str] = []
    current = ""
    for part in parts:
        if nbytes(part) > limit_bytes:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(part)  # 单卡超限，独立成块，交给上层告警
            continue
        if current and nbytes(current + part) > limit_bytes:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks
