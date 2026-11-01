"""把 candidates.json 渲染成 GitHub Step Summary（markdown 表格）。

只读 JSON、只写 stdout，方便在 workflow 里 `>> $GITHUB_STEP_SUMMARY`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _fmt_drop(stats: dict) -> str:
    dropped = stats.get("dropped") or {}
    if not dropped:
        return "—"
    return "、".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda kv: -kv[1]))


def render(data: dict) -> str:
    gh = data.get("github") or {}
    lines = [
        f"## M1 采集结果（{data.get('date', '?')}）",
        "",
        "| 栏目 | 最终/目标 | 原始 | 过滤后 | 去重 | 补位 | 缺口 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    total_kept = 0
    for col in data.get("columns", []):
        st = col.get("stats") or {}
        total_kept += st.get("kept", 0)
        mark = "❌ " if col.get("error") else ""
        lines.append(
            f"| {mark}{col.get('name')}（`{col.get('id')}`） "
            f"| {st.get('kept', 0)}/{col.get('target', 5)} "
            f"| {st.get('raw', 0)} | {st.get('qualified', 0)} | {st.get('deduped', 0)} "
            f"| {st.get('backfilled', 0)} | {st.get('shortfall', 0)} "
            f"| {st.get('duration_s', 0)}s |"
        )
    lines += [
        "",
        f"**合计**：{total_kept} 条候选 · 总耗时 {data.get('duration_s', 0)}s · "
        f"GitHub API 调用 {gh.get('api_calls', 0)} 次 · "
        f"{'已认证' if gh.get('authenticated') else '未认证'}",
        "",
    ]

    for col in data.get("columns", []):
        st = col.get("stats") or {}
        if col.get("error"):
            lines += [f"### {col.get('name')}：采集失败", "", f"```text\n{col['error']}\n```", ""]
            continue
        lines += [f"### {col.get('name')}（{st.get('kept', 0)} 条）", ""]
        lines += [f"- 丢弃：{_fmt_drop(st)}"]
        lines += [f"- README 来源：raw {st.get('readme_source', {}).get('raw', 0)} / "
                  f"API {st.get('readme_source', {}).get('api', 0)} / "
                  f"未取到 {st.get('readme_missing', 0)}"]
        lines += [""]
        for i, item in enumerate(col.get("items", []), 1):
            stars = item.get("stars", 0)
            stars_txt = f"{stars / 1000:.1f}k" if stars >= 1000 else str(stars)
            lines.append(
                f"{i}. [{item.get('full_name')}]({item.get('url')}) · ⭐{stars_txt} · "
                f"{item.get('language') or '未标注'} · README {item.get('readme_chars') or 0:,} 字符"
            )
        lines += [""]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "build/candidates.json")
    if not path.exists():
        print(f"⚠️ 没有找到候选文件：{path}（采集阶段可能提前失败）")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    sys.stdout.write(render(data))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv))
