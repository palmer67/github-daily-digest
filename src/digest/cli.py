"""采集层命令行入口。

用法：
  python scripts/collect.py --check-config                      # 只校验配置
  python scripts/collect.py --columns trending,rising           # 只跑部分栏目
  python scripts/collect.py --out build/candidates.json         # 落 JSON

退出码（方便 Actions 判定）：
  0 全部达标   2 配置错误   3 有栏目采集失败   4 有栏目不足 --require-min 条
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import Config, ConfigError, load_config
from .filters import describe_reason
from .github import GitHubClient
from .pipeline import ColumnResult, drop_summary, run_column
from .state import load_state

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_COLUMN_ERROR = 3
EXIT_SHORTFALL = 4

SCHEMA_VERSION = 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="collect",
        description="GitHub 每日开源速递 · M1 采集层（抓取 + 质量过滤 + 去重补位 → JSON）",
    )
    p.add_argument("--config", default="config/columns.yaml", help="栏目配置路径")
    p.add_argument("--columns", default="", help="只跑这些栏目 id，逗号分隔；空=全部启用的栏目")
    p.add_argument("--date", default="", help="按哪天算（YYYY-MM-DD），默认今天")
    p.add_argument("--out", default="", help="候选 JSON 输出路径，默认 build/candidates-<日期>.json")
    p.add_argument("--state", default="state/sent.json", help="去重状态文件（M1 只读）")
    p.add_argument("--require-min", type=int, default=5, help="每个栏目至少要有多少条，0=不检查")
    p.add_argument("--check-config", action="store_true", help="只校验配置并打印栏目清单")
    p.add_argument("--quiet", action="store_true", help="只打印汇总，不打印每条候选")
    return p.parse_args(argv)


def _select_columns(config: Config, wanted: str) -> list:
    enabled = config.enabled_columns
    if not wanted.strip():
        return enabled
    ids = [x.strip() for x in wanted.split(",") if x.strip()]
    known = config.columns_by_id()
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ConfigError(
            f"--columns 里有不存在的栏目：{', '.join(unknown)}；可用：{', '.join(known)}"
        )
    picked = [known[i] for i in ids]
    off = [c.id for c in picked if not c.enabled]
    if off:
        raise ConfigError(f"栏目 {', '.join(off)} 在配置里是 enabled: false，不跑")
    return picked


def _print_column(result: ColumnResult, elapsed: float, quiet: bool) -> None:
    col = result.column
    st = result.stats
    if result.error:
        print(f"  ❌ {col.id} 采集失败：{result.error}")
        return
    print(
        f"  {st['raw']} 条原始 → 过滤后 {st['qualified']} 条 → 去重 {st['deduped']} 条 "
        f"→ 最终 {st['kept']} 条（缺口 {st['shortfall']}，补位 {st['backfilled']}，{elapsed:.1f}s）"
    )
    print(f"  丢弃：{drop_summary(st)}")
    if st.get("readme_missing"):
        print(f"  README 未取到：{st['readme_missing']} 条（不计入不合格，交由 M2 降级处理）")
    if quiet:
        return
    for i, c in enumerate(result.items, 1):
        stars = f"{c.stars / 1000:.1f}k" if c.stars >= 1000 else str(c.stars)
        today = f"（今日 +{c.stars_today}）" if c.stars_today else ""
        readme = f"README {c.readme_chars:,} 字符" if c.readme_chars else "README 未知"
        print(f"   {i}. {c.full_name}  ⭐{stars}{today} · {c.language or '未标注'} · {readme}")


def _safe_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，遇到 ✅/⭐ 会直接崩；Actions 是 UTF-8，这里统一兜住。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _safe_utf8_stdout()
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
        columns = _select_columns(config, args.columns) if not args.check_config else []
    except ConfigError as e:
        print(f"❌ 配置错误\n{e}", file=sys.stderr)
        return EXIT_CONFIG

    print("=" * 64)
    print("M1 采集层")
    print("=" * 64)
    print(f"配置文件    : {args.config}")
    print(f"栏目        : {len(config.columns)} 个（启用 {len(config.enabled_columns)} 个）")
    print(f"每栏目目标  : {config.global_cfg.items_per_column} 条")
    print(f"去重窗口    : {config.global_cfg.dedupe_days} 天（栏目可覆盖）")

    if args.check_config:
        print("-" * 64)
        for c in config.enabled_columns:
            print(
                f"  ✅ {c.id:<14} {c.name:<8} source={c.source:<16} sort={c.sort:<15} "
                f"max={config.items_per_column(c)} pool={c.pool}"
            )
        for c in config.columns:
            if not c.enabled:
                print(f"  ⏸  {c.id:<14} {c.name:<8} enabled: false，跳过")
        print("-" * 64)
        print("✅ 配置校验通过")
        return EXIT_OK

    today = date.fromisoformat(args.date) if args.date else date.today()
    out_path = Path(args.out) if args.out else Path("build") / f"candidates-{today.isoformat()}.json"
    sent = load_state(args.state) if Path(args.state).exists() else {}
    if sent:
        print(f"状态文件    : {args.state}（{len(sent)} 条历史记录，M1 不写回）")

    client = GitHubClient()
    print(f"GitHub 认证 : {'已认证（GITHUB_TOKEN）' if client.authenticated else '未认证（限流 60 次/小时，可能不够）'}")
    print("-" * 64)

    started = time.time()
    results: list[ColumnResult] = []
    for col in columns:
        print(f"[{col.id}] {col.name}")
        t0 = time.time()
        res = run_column(client, config, col, sent, today)
        elapsed = time.time() - t0
        res.stats["duration_s"] = round(elapsed, 2)
        _print_column(res, elapsed, args.quiet)
        results.append(res)
        print()

    duration = time.time() - started
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "duration_s": round(duration, 2),
        "github": {
            "authenticated": client.authenticated,
            "api_calls": client.calls,
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        },
        "columns": [
            {
                "id": r.column.id,
                "name": r.column.name,
                "source": r.column.source,
                "sort": r.column.sort,
                "target": config.items_per_column(r.column),
                "error": r.error,
                "stats": r.stats,
                "items": [c.to_dict() for c in r.items],
            }
            for r in results
        ],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---------- 汇总 ----------
    print("=" * 64)
    print("汇总")
    print("=" * 64)
    failed = [r.column.id for r in results if r.error]
    short = [f"{r.column.id}({r.stats['kept']}/{config.items_per_column(r.column)})" for r in results if r.shortfall]
    for r in results:
        mark = "❌" if r.error else ("⚠️" if r.shortfall else "✅")
        print(f"  {mark}  {r.column.id:<14} {r.stats.get('kept', 0):>2} 条  {r.column.name}")
    print(f"候选 JSON   : {out_path}（{out_path.stat().st_size:,} 字节）")
    print(f"总耗时      : {duration:.1f}s，GitHub API 调用 {client.calls} 次")

    if failed:
        print(f"\n❌ 有栏目采集失败：{', '.join(failed)}")
        return EXIT_COLUMN_ERROR
    if args.require_min and short:
        print(f"\n❌ 以下栏目不足 {args.require_min} 条：{', '.join(short)}")
        print("   补位已开启时仍不足，说明该查询今天真的没有更多合格项目（7.3：照实推送并标注，不拿历史凑数）")
        return EXIT_SHORTFALL
    print("\n✅ M1 采集通过")
    return EXIT_OK
