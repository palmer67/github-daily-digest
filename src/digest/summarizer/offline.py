"""离线桩客户端：不调模型，用来验管道 / 跑单测。

放在 src 里（原来只在 scripts/summarize.py 里），M4 的 daily 流程也要用。
"""

from __future__ import annotations

from typing import Any

from .deepseek import DeepSeekClient


class OfflineClient(DeepSeekClient):
    """按简介拼一张卡，专门验管道和渲染，绝不发网络请求。"""

    def __init__(self) -> None:
        super().__init__(api_key="offline", transport=lambda *a, **k: (200, "{}"))

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls += 1
        self.usage["prompt_tokens"] += len(system + user) // 4
        self.usage["completion_tokens"] += 90
        repo = user.split("仓库：", 1)[1].split("\n", 1)[0] if "仓库：" in user else "unknown/repo"
        desc = user.split("简介：", 1)[1].split("\n", 1)[0] if "简介：" in user else ""
        return {
            "what": desc[:60] or f"{repo} 是一个开源项目",
            "value": "离线模式占位：真实卡片由 DeepSeek 生成",
            "cost": {"price": "免费", "needs_api_key": False, "needs_server": False, "needs_gpu": False},
            "difficulty": 2,
            "pitfall": "",
            "cost_basis": "",
        }
