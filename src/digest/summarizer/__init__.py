"""摘要层的编排：裁剪 README → 调模型 → 校验 → 不合格就带问题清单重问一次 → 难度兜底。

失败策略（策划书 7.4）：
- 单卡失败不影响整批：这一张降级成「原始简介 + 今日未能生成通俗总结」。
- 401/402 是账号级问题，后面所有卡都会一样失败，所以直接抛给上层（上层负责全员降级 + 告警）。
- 被限流/超时：这一张重试 3 次后降级，不拖住整批。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..models import Candidate
from ..readme import trim_for_prompt
from . import difficulty
from .deepseek import DeepSeekClient, LLMAuthError, LLMError, LLMFormatError
from .prompt import SYSTEM_PROMPT, build_user_message, corrective_hint
from .schema import Card, fallback_card, unspecified_fields, validate


@dataclass
class SummaryResult:
    card: Card
    ok: bool
    attempts: int = 1
    issues: list[str] = field(default_factory=list)
    trim_note: str = ""
    error: str = ""

    def to_dict(self, cand: Candidate) -> dict[str, Any]:
        return {
            "full_name": cand.full_name,
            "stars": cand.stars,
            "language": cand.language,
            "url": cand.html_url or f"https://github.com/{cand.full_name}",
            "stars_today": cand.stars_today,
            "ok": self.ok,
            "attempts": self.attempts,
            "issues": self.issues,
            "trim_note": self.trim_note,
            "error": self.error,
            "card": self.card.model_dump(),
        }


def summarize(
    client: DeepSeekClient,
    cand: Candidate,
    readme_text: str | None,
    max_field_retries: int = 1,
) -> SummaryResult:
    """给一个候选项目生成卡片。不抛业务异常（除 LLMAuthError 这类账号级错误）。"""
    if not readme_text or len(readme_text.strip()) < 50:
        card = fallback_card(cand, "README 太短或没取到，无法总结")
        return SummaryResult(card=card, ok=False, trim_note="missing", error="README missing")

    trimmed, trim_note = trim_for_prompt(readme_text)
    user = build_user_message(cand, trimmed, trim_note)

    attempts = 0
    issues: list[str] = []
    while attempts <= max_field_retries:
        attempts += 1
        try:
            data = client.chat_json(SYSTEM_PROMPT, user)
        except LLMAuthError:
            raise
        except LLMFormatError as e:
            issues = [f"输出不是合法 JSON：{e}"]
            user = build_user_message(cand, trimmed, trim_note) + corrective_hint(issues)
            continue
        except LLMError as e:
            card = fallback_card(cand, "模型调用失败")
            return SummaryResult(card=card, ok=False, attempts=attempts, error=str(e), trim_note=trim_note)

        try:
            card = Card.model_validate(data)
        except ValidationError as e:
            issues = [f"字段不符合格式：{_brief(e)}"]
            user = build_user_message(cand, trimmed, trim_note) + corrective_hint(issues)
            continue

        issues = validate(card, readme_text)
        if issues:
            user = build_user_message(cand, trimmed, trim_note) + corrective_hint(issues)
            continue

        level, basis = difficulty.reconcile(card.difficulty, trimmed)
        card.difficulty = level
        card.difficulty_basis = basis
        return SummaryResult(card=card, ok=True, attempts=attempts, issues=[], trim_note=trim_note)

    # 重问过还是不合格：降级成原始简介，把问题清单带出去（上层可据此统计）
    card = fallback_card(cand, "格式/内容校验两次都没过")
    return SummaryResult(card=card, ok=False, attempts=attempts, issues=issues, error="; ".join(issues), trim_note=trim_note)


def _brief(error: ValidationError) -> str:
    parts = []
    for e in error.errors()[:3]:
        loc = ".".join(str(x) for x in e.get("loc", ()))
        parts.append(f"{loc or '整体'}: {e.get('msg', '')}")
    return "；".join(parts)


__all__ = [
    "Card",
    "DeepSeekClient",
    "LLMAuthError",
    "SummaryResult",
    "summarize",
    "unspecified_fields",
    "SYSTEM_PROMPT",
]
