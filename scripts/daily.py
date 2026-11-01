"""M4/M5：每日速递入口（采集 → 摘要 → 渲染 → 推送 → 写状态 → 心跳）。

用法：
  python scripts/daily.py --dry-run          # 全流程但不推送、不写状态
  python scripts/daily.py                    # 真跑：推送 + 写 state/sent.json + 发心跳
  python scripts/daily.py --backup           # 备用 cron：今天推过就秒退，没推过才补跑
  python scripts/daily.py --status           # 只看今天的状态与心跳，不跑流程
  python scripts/daily.py --force            # 今天已经推过也再推一次（排障用）

退出码：0 成功（含「今天已推过，秒退」）／2 配置或输入错误／3 全部栏目采集失败／5 推送失败。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.config import ConfigError, load_config  # noqa: E402
from digest.daily import run_daily  # noqa: E402
from digest.github import GitHubClient  # noqa: E402
from digest.notifiers import NotifierError, WeComNotifier  # noqa: E402
from digest.summarizer.deepseek import DeepSeekClient  # noqa: E402
from digest.summarizer.offline import OfflineClient  # noqa: E402
from digest import monitor  # noqa: E402
from digest.state import already_pushed, load_state_doc  # noqa: E402

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_COLLECT = 3
EXIT_PUSH = 5


def _safe_stdout() -> None:
    """Windows 控制台是 GBK，打印 ✅ / ⭐ 会炸，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="每日开源速递：采集 → 摘要 → 推送 → 记状态")
    p.add_argument("--config", default="config/columns.yaml")
    p.add_argument("--state", default="state/sent.json")
    p.add_argument("--date", default="", help="默认今天（Asia/Shanghai，由 workflow 决定）")
    p.add_argument("--dry-run", action="store_true", help="不推送、不写状态")
    p.add_argument("--force", action="store_true", help="今天已推过也再推一次")
    p.add_argument("--offline", action="store_true", help="用离线桩代替 DeepSeek，只验管道")
    p.add_argument("--webhook", default="", help="覆盖 WECOM_WEBHOOK_URL（排障用）")
    p.add_argument("--columns", default="", help="只跑指定栏目（逗号分隔，验收/补跑用）")
    p.add_argument("--require-push", action="store_true", help="要求至少推送成功 1 条，否则退出 5")
    p.add_argument("--backup", action="store_true", help="备用 cron 模式：今天推过就秒退，没推过才补跑")
    p.add_argument("--status", action="store_true", help="只打印今天的状态与心跳，不跑流程")
    p.add_argument("--stale-demo", action="store_true", help="演示心跳漏报时的告警文案（不联网、不跑流程）")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _safe_stdout()
    args = _parse_args(argv)
    day = args.date or date.today().isoformat()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"❌ 配置有问题：{e}")
        return EXIT_INPUT

    if args.status:
        doc = load_state_doc(args.state)
        hb = monitor.read_heartbeat(Path(args.state).parent)
        today = day
        run = doc.get("runs", {}).get(today, {})
        print(f"日期        : {today}")
        print(f"今天推送过  : {'是' if already_pushed(doc, today) else '否'}")
        print(f"runs 记录   : {run or '（无）'}")
        print(f"心跳        : {hb or '（没有 heartbeat.json）'}")
        print(f"心跳地址    : {'已配置' if monitor.heartbeat_url() else '未配置（跳过外发）'}")
        return EXIT_OK

    if args.stale_demo:
        hb = monitor.read_heartbeat(Path(args.state).parent)
        print(monitor.stale_alert(day, hb))
        return EXIT_OK

    if args.backup:
        doc = load_state_doc(args.state)
        if already_pushed(doc, day):
            print(f"⏭️  备用 cron：{day} 已经推送过，秒退（不重复推送）")
            return EXIT_OK
        hb = monitor.read_heartbeat(Path(args.state).parent)
        if hb.get("date") != day:
            print(f"⚠️  备用 cron：{day} 还没有成功心跳（最近 {hb.get('date') or '无记录'}），本次补跑")
        else:
            print(f"⚠️  备用 cron：有今天的 {day} 心跳但 runs 没记推送成功，本次补跑")

    if args.offline:
        client = OfflineClient()
        source = "离线桩"
    else:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            print("❌ 没有 DEEPSEEK_API_KEY（真实跑需要；只想验管道加 --offline）")
            return EXIT_INPUT
        client = DeepSeekClient(api_key=key)
        source = f"DeepSeek（{client.model}）"

    notifier = None
    if not args.dry_run:
        url = args.webhook.strip() or os.environ.get("WECOM_WEBHOOK_URL", "").strip()
        try:
            notifier = WeComNotifier(url)
        except NotifierError as e:
            print(f"❌ 推送器没配好：{e}")
            return EXIT_INPUT

    print("=" * 64)
    print("每日开源速递 · M5")
    print("=" * 64)
    print(f"模型        : {source}")
    print(f"配置        : {args.config}（{len(config.enabled_columns)} 个启用栏目）")
    if notifier is not None:
        print(f"推送目标    : {notifier.masked}")
    print()

    gh = GitHubClient()
    try:
        report = run_daily(
            args.config,
            args.state,
            day,
            gh=gh,
            client=client,
            notifier=notifier,
            columns=[c.strip() for c in args.columns.split(",") if c.strip()] or None,
            dry_run=args.dry_run,
            force=args.force,
        )
    except ConfigError as e:
        print(f"❌ 配置有问题：{e}")
        return EXIT_INPUT
    except (OSError, ValueError) as e:
        print(f"❌ 读状态文件失败：{e}")
        return EXIT_INPUT

    if report.skipped:
        return EXIT_OK
    if all(c.error for c in report.columns) and report.columns:
        return EXIT_COLLECT
    if report.push_error:
        return EXIT_PUSH
    if args.require_push and report.pushed == 0:
        print("❌ --require-push 要求至少推送 1 条，实际 0 条")
        return EXIT_PUSH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
