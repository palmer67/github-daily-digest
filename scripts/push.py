"""M3 渲染与推送入口。

三件事：
1. 把 cards JSON 渲染成企业微信 markdown（按栏目分段，按 4000 字节切，不劈卡片）；
2. dry-run 打印每条消息的字节数与卡片归属（先看，别急着发）；
3. `--send` 真发，**必须判 errcode == 0**（策划书 7.1）。

退出码：0 成功；2 参数/文件问题；3 推送失败（含 errcode != 0）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from digest.config import ConfigError, load_config  # noqa: E402
from digest.models import Candidate  # noqa: E402
from digest.notifiers import NotifierError, WeComError, WeComNotifier  # noqa: E402
from digest.renderer import CARD_SEP, nbytes, render_messages, split_markdown  # noqa: E402
from digest.summarizer.schema import Card  # noqa: E402


def _safe_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _load_cards(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        raise SystemExit(f"找不到卡片文件：{path}（先跑 scripts/summarize.py）")
    data = json.loads(path.read_text(encoding="utf-8"))
    cards = data.get("cards") or []
    if not cards:
        raise SystemExit(f"{path} 里没有卡片")
    return data, cards


def _candidate(entry: dict[str, Any]) -> Candidate:
    return Candidate(
        full_name=entry["full_name"],
        source=entry.get("column", entry.get("source", "")),
        description="",
        language=entry.get("language") or "",
        stars=int(entry.get("stars") or 0),
        forks=int(entry.get("forks") or 0),
        topics=[],
        pushed_at="",
        created_at="",
        stars_today=int(entry.get("stars_today") or 0),
        readme_chars=None,
        html_url=entry.get("url") or f"https://github.com/{entry['full_name']}",
    )


def _group(
    cards: list[dict[str, Any]], column_names: dict[str, str], order: list[str]
) -> list[tuple[str, str, list[tuple[Candidate, Card]]]]:
    buckets: dict[str, list[tuple[Candidate, Card]]] = {}
    for entry in cards:
        col = entry.get("column") or "other"
        card = Card.model_validate(entry["card"])
        buckets.setdefault(col, []).append((_candidate(entry), card))
    ordered = [c for c in order if c in buckets] + [c for c in buckets if c not in order]
    return [(c, column_names.get(c, c), buckets[c]) for c in ordered]


def _print_messages(result: Any, limit_bytes: int) -> None:
    print("=" * 64)
    print("渲染结果")
    print("=" * 64)
    for m in result.messages:
        flag = "✅" if m.nbytes <= limit_bytes else "❌ 超限"
        title = m.text.splitlines()[0]
        print(
            f"  {flag} [{m.column_name}] {m.part}/{m.parts} 段　{m.nbytes} 字节　{title}"
        )
    print("-" * 64)
    print(f"  共 {len(result.messages)} 条消息，合计 {result.total_bytes} 字节")
    if result.warnings:
        print("\n⚠️ 警告")
        for w in result.warnings:
            print(f"  - {w}")


def _verify_cards_intact(result: Any) -> bool:
    """每条消息里 `### ` 开头都是完整卡片：第 1 条之外的段不得以卡片中段开头。"""
    ok = True
    for m in result.messages:
        body = m.text.split("\n", 1)[1] if "\n" in m.text else ""
        if body and not body.startswith(CARD_SEP.lstrip("\n")) and not body.startswith("### "):
            ok = False
            print(f"  ❌ [{m.column_name}] 第 {m.part} 段不是从卡片边界开始")
        count = body.count("\n### ")
        if count == 0 and body.strip() and not body.lstrip().startswith("### "):
            ok = False
            print(f"  ❌ [{m.column_name}] 第 {m.part} 段里没有任何卡片")
    return ok


def _split_demo(limit_bytes: int) -> int:
    """验收用：造 5 张超长卡，证明拆多条且卡片完整。"""
    from digest.renderer import render_card  # 局部导入，避免脚本启动就依赖摘要层

    long_text = "这是一个故意写得很长的说明，用来把整条消息撑爆，验证拆分逻辑只会在卡片边界上切。" * 12
    items: list[tuple[Candidate, Card]] = []
    for i in range(5):
        cand = Candidate(
            full_name=f"demo/oversize-project-{i + 1}",
            source="demo",
            description="demo",
            language="Python",
            stars=12345 + i,
            forks=100,
            topics=[],
            pushed_at="",
            created_at="",
            stars_today=99,
            readme_chars=2000,
            html_url=f"https://github.com/demo/oversize-project-{i + 1}",
        )
        card = Card.model_validate(
            {
                "what": long_text,
                "value": long_text,
                "cost": {
                    "price": "免费",
                    "needs_api_key": False,
                    "needs_server": False,
                    "needs_gpu": False,
                },
                "difficulty": 2,
                "pitfall": long_text[:200],
            }
        )
        items.append((cand, card))

    full = "\n".join(render_card(i, c, card) for i, (c, card) in enumerate(items, 1))
    print(f"整段 {nbytes(full)} 字节（企业微信上限 4096），按 {limit_bytes} 字节切分")

    result = render_messages("2026-11-01", [("demo", "超长演示", items)], limit_bytes)
    _print_messages(result, limit_bytes)

    # 逐段检查：每张卡的标题行必须完整出现在同一段里
    ok = True
    for m in result.messages:
        names = [c.full_name for c, _ in items if c.full_name in m.text]
        heads = [c.full_name for c, _ in items if f"### " in m.text and c.full_name in m.text.split("\n### ", 1)[0]]
        _ = heads
        if names:
            print(f"  段 {m.part}: 含 {len(names)} 张卡 → {', '.join(names)}")
    for c, _card in items:
        hits = [m.part for m in result.messages if c.full_name in m.text]
        if len(hits) != 1:
            ok = False
            print(f"  ❌ {c.full_name} 出现在 {hits}（跨段即被劈开）")
    print("✅ 拆分只在卡片边界发生，每张卡完整落在同一段" if ok else "❌ 有卡片被劈开")
    return 0 if ok else 3


def main(argv: Sequence[str] | None = None) -> int:
    _safe_utf8_stdout()
    p = argparse.ArgumentParser(description="M3 渲染与推送")
    p.add_argument("--cards", default="", help="cards JSON，默认 build/cards-<日期>.json")
    p.add_argument("--config", default=str(ROOT / "config" / "columns.yaml"))
    p.add_argument("--date", default="", help="标题里的日期，默认取 cards 文件里的")
    p.add_argument("--limit-bytes", type=int, default=0, help="单条消息上限，默认取配置 max_message_bytes")
    p.add_argument("--send", action="store_true", help="真的发到企业微信（不传就是 dry-run）")
    p.add_argument("--webhook", default="", help="企业微信 webhook，默认读环境变量 WECOM_WEBHOOK_URL")
    p.add_argument("--split-demo", action="store_true", help="验收演示：造超长消息验证拆分")
    args = p.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except ConfigError as e:
        print(f"❌ 配置有问题：\n{e}", file=sys.stderr)
        return 2

    limit = args.limit_bytes or config.global_cfg.max_message_bytes

    if args.split_demo:
        return _split_demo(limit)

    cards_path = Path(args.cards) if args.cards else None
    if cards_path is None:
        print("❌ 必须给 --cards（或用 --split-demo）", file=sys.stderr)
        return 2

    data, cards = _load_cards(cards_path)
    date = args.date or str(data.get("generated_at", ""))[:10] or "今天"
    order = [c.id for c in config.columns if c.enabled]
    names = {c.id: c.name for c in config.columns}
    columns = _group(cards, names, order)

    result = render_messages(date, columns, limit)
    _print_messages(result, limit)
    intact = _verify_cards_intact(result)
    if not intact:
        return 3

    if not args.send:
        print("\n（dry-run：没有真的发送；加 --send 才推送到企业微信）")
        return 0

    webhook = args.webhook or os.environ.get("WECOM_WEBHOOK_URL", "")
    try:
        notifier = WeComNotifier(webhook)
    except NotifierError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 3

    print(f"\n开始推送 → {notifier.masked}")
    try:
        for m in result.messages:
            body = notifier.send_markdown(m.text)
            print(
                f"  ✅ [{m.column_name}] {m.part}/{m.parts} 段　{m.nbytes} 字节　"
                f"errcode={body.get('errcode')} errmsg={body.get('errmsg')}"
            )
    except WeComError as e:
        print(f"  ❌ 推送失败：errcode={e.errcode} errmsg={e.errmsg} {e.hint}", file=sys.stderr)
        return 3
    except NotifierError as e:
        print(f"  ❌ 推送失败：{e}", file=sys.stderr)
        return 3

    print(f"\n✅ 全部推送成功：{notifier.sent}/{len(result.messages)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
