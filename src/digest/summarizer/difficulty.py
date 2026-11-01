"""难度字段的机械兜底（3.3）。

背景：v1.0 用纯提示词问「上手难度 1–5」，5 张真实卡片全输出 3，这个字段等于废了（A-15）。
现在两条一起上：
  1. 提示词里写死 5 个锚点（prompt.py）
  2. 这里用正则从 README 提信号；信号比模型给的更高时，以信号为准

为什么是「抬高」而不是「覆盖」：信号只能证明「至少这么难」（出现 CUDA 就一定 ≥5），
但证明不了「没这么难」——README 完全可能在信号之外还有更复杂的步骤。所以取两者的较大值，
既挡住模型偷懒给 3，又不会把模型看到的真实复杂度压下去。
"""

from __future__ import annotations

import re

# 正则 → (难度下限, 中文理由)。按难度从高到低匹配，最后取最大值。
SIGNALS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\bCUDA\b|\bVRAM\b|\bA100\b|\bRTX\b|\bH100\b|\b24\s?GB\b", re.IGNORECASE), 5, "提到显卡 / 显存"),
    (re.compile(r"docker[\s-]?compose|\bpostgres(ql)?\b|\bredis\b|reverse proxy|nginx", re.IGNORECASE), 4, "要自建服务端 / 数据库"),
    (re.compile(r"api[\s_-]?key|\bapikey\b|\baccess token\b|\.env\b", re.IGNORECASE), 3, "要申请 Key 或改配置"),
    (re.compile(r"pip install|npm (i|install)|cargo (build|install)|pnpm (add|install)|go install|yarn (add|install)", re.IGNORECASE), 2, "要装依赖"),
    (re.compile(r"docker run|npx |brew install", re.IGNORECASE), 1, "一条命令起"),
)

# 明确说不需要显卡时，别被「GPU」这个词误伤
NO_GPU_RE = re.compile(
    r"no\s+(gpu|cuda)|不需要\s*(显卡|GPU)|无需\s*(显卡|GPU)|\bcpu[\s-]?only\b|不支持\s*GPU",
    re.IGNORECASE,
)


def detect(text: str) -> tuple[int, list[str]]:
    """返回 (信号给出的难度下限, 命中的理由列表)。没命中返回 (0, [])。"""
    if not text:
        return 0, []
    level = 0
    reasons: list[str] = []
    negate_gpu = bool(NO_GPU_RE.search(text))
    for pattern, floor, why in SIGNALS:
        if negate_gpu and floor == 5:
            continue
        if pattern.search(text):
            reasons.append(f"{why}（≥{floor}）")
            level = max(level, floor)
    return level, reasons


def reconcile(llm_level: int, readme_text: str) -> tuple[int, str]:
    """把模型给的难度和信号对齐，返回 (最终难度, 兜底说明)。"""
    level, reasons = detect(readme_text)
    if level > llm_level:
        return level, f"模型给 {llm_level}，README 出现 {' / '.join(reasons)}，机械抬到 {level}"
    return llm_level, ""
