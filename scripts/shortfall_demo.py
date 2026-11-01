"""M4 验收第 4 条：补不齐时消息里必须如实标注「今日仅 N 条」。

云端没法等一周把候选池自然耗尽，所以这里用一个受控实验：
  1) 真实采一次某栏目的原始候选（走真 API，拿到这一栏今天真正能看到的项目）
  2) 把这些项目全部标成「今天已经推过」，只留 2 个不放回
  3) 再跑一次同一栏目 → 必然凑不齐 items_per_column 条
  4) 打印渲染出来的消息头，检查有没有「今日仅 2 条」
整个过程**不推送**，只读 API。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.config import load_config  # noqa: E402
from digest.daily import _shortfall_note  # noqa: E402
from digest.github import GitHubClient  # noqa: E402
from digest.models import Candidate  # noqa: E402
from digest.pipeline import collect_raw, run_column  # noqa: E402
from digest.renderer import render_messages  # noqa: E402
from digest.summarizer.offline import OfflineClient  # noqa: E402
from digest.summarizer import summarize  # noqa: E402


def main(argv: list[str]) -> int:
    column_id = argv[0] if argv else "freebies"
    config_path = argv[1] if len(argv) > 1 else "config/columns.yaml"
    today = date.today()

    config = load_config(config_path)
    column = config.columns_by_id()[column_id]
    gh = GitHubClient()
    need = config.items_per_column(column)

    raw = collect_raw(gh, column, today)
    print(f"栏目 {column_id}：今天原始候选 {len(raw)} 条，目标 {need} 条")
    if len(raw) < need:
        print("⚠️ 这个栏目今天本来就不够 5 条，直接看它自己的日志就行")
        return 0

    # 只留 2 条不放回，其余全部标记为「今天推过」
    keep_free = {c.full_name for c in raw[2:4]}
    sent = {c.full_name: today.isoformat() for c in raw if c.full_name not in keep_free}
    print(f"伪造状态：{len(sent)} 条今天推过，只放回 2 条：{sorted(keep_free)}")

    result = run_column(gh, config, column, sent, today, log=lambda *_: None)
    print(
        f"重跑结果：{len(result.items)}/{need} 条"
        f"（去重 {result.stats.get('deduped')}、顺延补位 {result.stats.get('backfilled')}、"
        f"缺口 {result.stats.get('shortfall')}）"
    )
    if not result.items:
        print("❌ 一条都没剩，没法验标注（池子被过滤光了）")
        return 1

    client = OfflineClient()
    pairs = [(c, summarize(client, c, "README 正文" * 120).card) for c in result.items]
    messages = render_messages(
        today.isoformat(),
        [(column.id, column.name, pairs)],
        notes={column.id: _shortfall_note(len(result.items), need)},
    )
    head = messages.messages[0].text.splitlines()[0]
    print(f"消息头：{head}")
    expected = f"今日仅 {len(result.items)} 条"
    if expected not in head:
        print(f"❌ 消息里没有「{expected}」")
        return 1
    print(f"✅ 补不齐时如实标注「{expected}」，没有拿历史凑数")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
