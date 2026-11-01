"""3.2 卡片渲染 + 3.6 消息拆分（M3）。

卡片格式照策划书 3.2 的五段：标题行、是什么、价值、成本、坑（可省略）、链接。

拆分规则（策划书 3.6）：企业微信 markdown 硬上限 4096 字节，按 4000 字节切，
**只在卡片边界 `\\n### ` 上切**，一张卡绝不允许劈成两半；单卡本身超限时独立成块
并记一条 warning，由调用方决定告警（不静默截断）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Candidate
from .summarizer.schema import UNSPECIFIED, Card

CARD_SEP = "\n### "

# 拆分时给「栏目名 + 续标题」留的余量（中文标题通常 10–20 字节）
HEADER_RESERVE = 60


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


def format_stars(stars: int) -> str:
    if stars >= 10000:
        return f"{stars / 1000:.1f}k"
    if stars >= 1000:
        return f"{stars / 1000:.1f}k"
    return str(stars)


def render_card(rank: int, cand: Candidate, card: Card) -> str:
    """一张卡。坑为「未说明」或空时整行省略（策划书 3.2）。"""
    bits = [f"### {rank}. {cand.full_name}"]
    meta = [f"⭐{format_stars(cand.stars)}"]
    if cand.stars_today:
        meta.append(f"今日 +{cand.stars_today:,}")
    if cand.language:
        meta.append(cand.language)
    bits[0] += "　" + " · ".join(meta)

    what = card.what.strip() or (cand.description or "").strip() or "README 没写简介"
    if card.degraded:
        what += "（⚠️ 自动摘要失败，以下为仓库原始简介）"
    bits.append(f"**是什么**：{what}")

    if card.value.strip():
        bits.append(f"**价值**：{card.value.strip()}")

    bits.append(f"**成本**：{card.cost_line()}")

    pitfall = card.pitfall.strip()
    if pitfall and pitfall != UNSPECIFIED:
        bits.append(f"**坑**：{pitfall}")

    bits.append(f"[👉 看项目]({cand.html_url})")
    return "\n".join(bits)


@dataclass
class RenderedMessage:
    text: str
    column_id: str
    column_name: str
    part: int
    parts: int

    @property
    def nbytes(self) -> int:
        return nbytes(self.text)


@dataclass
class RenderResult:
    messages: list[RenderedMessage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(m.nbytes for m in self.messages)


def render_messages(
    date: str,
    columns: list[tuple[str, str, list[tuple[Candidate, Card]]]],
    limit_bytes: int = 4000,
    notes: dict[str, str] | None = None,
) -> RenderResult:
    """把「栏目 → 卡片列表」渲染成若干条消息。

    columns 里每项是 (栏目 id, 栏目显示名, [(候选, 卡片), ...])。
    notes 是栏目级备注（例如去重后补不齐时的「今日仅 3 条」），拼在标题后面，
    只拼在该栏目的第一段上，免得续段标题也被拉长。
    """
    notes = notes or {}
    result = RenderResult()
    budget = limit_bytes - HEADER_RESERVE

    for column_id, column_name, items in columns:
        if not items:
            continue
        body = "\n".join(
            render_card(i, cand, card) for i, (cand, card) in enumerate(items, 1)
        )
        chunks = split_markdown(body, budget)
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            note = notes.get(column_id, "") if i == 0 else ""
            if note:
                note = f"　{note}"
            if i == 0:
                head = f"## {column_name}（{date}）{note}\n"
            else:
                head = f"## {column_name}（续 {i + 1}/{total}）\n"
            text = head + chunk
            if nbytes(text) > limit_bytes:
                result.warnings.append(
                    f"{column_name} 第 {i + 1}/{total} 段有 {nbytes(text)} 字节，超过 {limit_bytes}：单张卡本身太长，已原样保留"
                )
            result.messages.append(
                RenderedMessage(
                    text=text,
                    column_id=column_id,
                    column_name=column_name,
                    part=i + 1,
                    parts=total,
                )
            )
    return result
