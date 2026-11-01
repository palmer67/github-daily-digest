"""卡片的输出结构与校验（3.2 五段 + 3.4「未说明」白名单）。

这里的校验分两层：
- pydantic 管「形状」：字段不能少、不能多、取值要在允许范围内。
- validate() 管「内容」：空话、过长、README 明明有线索却写「未说明」。
模型输出不合格时不是直接扔掉，而是带着问题清单再问一次（见 service.py）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: F401  (ValidationError 供外部使用)

from ..models import Candidate

UNSPECIFIED = "未说明"
PRICES = ("免费", "部分免费", "付费", UNSPECIFIED)
BoolOrUnknown = bool | Literal["未说明"]

# 每个成本字段各自认哪些「线索词」。
# 为什么要分开：早先所有字段共用一个正则，README 里出现 "license" 就让
# needs_gpu 的「未说明」被判成偷懒，模型补不出来 → 整张卡降级。
# 线索必须指向那个字段本身，才算「README 说了但你没读」。
FIELD_EVIDENCE: dict[str, re.Pattern[str]] = {
    "price": re.compile(
        r"pricing|\bprice\b|\bfree\b|\bpaid\b|\blicense\b|subscription|收费|免费|付费|订阅",
        re.IGNORECASE,
    ),
    "needs_api_key": re.compile(
        r"api[\s_-]?key|apikey|api[\s_-]?token|access[\s_-]?token|\btoken\b|鉴权|密钥|令牌",
        re.IGNORECASE,
    ),
    "needs_server": re.compile(
        r"docker|docker-compose|self-host|selfhost|\bdeploy\b|\bserver\b|nginx|postgres|redis|supabase|\bhosted\b",
        re.IGNORECASE,
    ),
    "needs_gpu": re.compile(
        r"\bgpu\b|\bcuda\b|\bvram\b|nvidia|a100|h100|rtx|\b显存\b|显卡",
        re.IGNORECASE,
    ),
}


MAX_FIELD_CHARS = 120


class CostInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: str = Field(default=UNSPECIFIED)
    needs_api_key: BoolOrUnknown = UNSPECIFIED
    needs_server: BoolOrUnknown = UNSPECIFIED
    needs_gpu: BoolOrUnknown = UNSPECIFIED


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = ""
    value: str = ""
    cost: CostInfo = Field(default_factory=CostInfo)
    difficulty: int = Field(default=3, ge=1, le=5)
    pitfall: str = ""
    cost_basis: str = ""

    # 由我们填，不是模型输出
    degraded: bool = False
    degraded_reason: str = ""
    difficulty_basis: str = ""

    def cost_line(self) -> str:
        """渲染成「免费 · 要 API Key · 要自己起服务 · 难度 3」这种一行。"""
        parts: list[str] = [self.cost.price or UNSPECIFIED]
        parts.append({"True": "要 API Key", "False": "不用 API Key"}.get(str(self.cost.needs_api_key), f"API Key {self.cost.needs_api_key}"))
        parts.append({"True": "要自己起服务", "False": "不用起服务"}.get(str(self.cost.needs_server), f"服务端 {self.cost.needs_server}"))
        parts.append({"True": "要显卡", "False": "不用显卡"}.get(str(self.cost.needs_gpu), f"显卡 {self.cost.needs_gpu}"))
        parts.append(f"上手难度 {self.difficulty}")
        return " · ".join(parts)


def unspecified_fields(card: Card) -> list[str]:
    out: list[str] = []
    if card.cost.price == UNSPECIFIED:
        out.append("price")
    for name, value in (
        ("needs_api_key", card.cost.needs_api_key),
        ("needs_server", card.cost.needs_server),
        ("needs_gpu", card.cost.needs_gpu),
    ):
        if value == UNSPECIFIED:
            out.append(name)
    return out


def field_evidence(readme_text: str) -> dict[str, list[str]]:
    """逐个成本字段找线索：{"needs_gpu": ["cuda", "vram"], ...}（只含命中的字段）。"""
    text = readme_text or ""
    out: dict[str, list[str]] = {}
    for field, pattern in FIELD_EVIDENCE.items():
        hits: list[str] = []
        for m in pattern.finditer(text):
            word = m.group(0).strip().lower()
            if word and word not in hits:
                hits.append(word)
        if hits:
            out[field] = hits
    return out


def has_cost_evidence(readme_text: str) -> list[str]:
    """README 里出现的所有成本线索词（去重后返回，给日志/测试用）。"""
    found: list[str] = []
    for hits in field_evidence(readme_text).values():
        for word in hits:
            if word not in found:
                found.append(word)
    return found


def validate(card: Card, readme_text: str) -> list[str]:
    """内容层校验，返回中文问题清单（空列表 = 通过）。"""
    issues: list[str] = []
    what = card.what.strip()
    value = card.value.strip()
    if len(what) < 4:
        issues.append("what 是空的，必须说明这个项目是什么")
    elif len(what) > MAX_FIELD_CHARS:
        issues.append(f"what 太长了（{len(what)} 字），压到 60 字以内")
    if len(value) < 4:
        issues.append("value 是空的，必须说明适合谁、解决什么问题")
    elif len(value) > MAX_FIELD_CHARS:
        issues.append(f"value 太长了（{len(value)} 字），压到 60 字以内")
    if len(card.pitfall) > MAX_FIELD_CHARS:
        issues.append(f"pitfall 太长了（{len(card.pitfall)} 字），压到 60 字以内")

    evidence = field_evidence(readme_text)
    for field in unspecified_fields(card):
        hits = evidence.get(field)
        if not hits:
            continue  # 白名单场景：README 没提这个字段，允许「未说明」
        issues.append(
            f"README 里提到了 {'、'.join(hits[:3])}，但 {field} 写了「未说明」；"
            "请据此给出具体判断，确实读不出来才保留「未说明」"
        )
    return issues


def unspecified_rate(cards: list[Card]) -> float:
    """所有成本字段里「未说明」的占比（验收要求 < 20%）。"""
    total = 0
    unknown = 0
    for c in cards:
        total += 4
        unknown += len(unspecified_fields(c))
    return unknown / total if total else 0.0


def fallback_card(cand: Candidate, reason: str) -> Card:
    """LLM 不可用时的降级卡片：只给原始简介，绝不编造（7.4）。"""
    brief = (cand.description or "").strip() or "README 没写简介"
    return Card(
        what=brief[:MAX_FIELD_CHARS],
        value="",
        cost=CostInfo(),
        difficulty=3,
        degraded=True,
        degraded_reason=reason,
    )
