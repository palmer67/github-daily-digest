"""守望：到了早上八点多还没推送成功，就主动在企业微信里喊一声。

为什么需要它：定时任务要是压根没触发（cron 时区写错、被平台禁用、任务失败），
群里只会「安静得没问题」，没人会发现。这个脚本每天北京时间 08:10 检查一次
state/sent.json，发现今天没有推送记录就发一条告警，并让 workflow 变红。

退出码：
  0  今天已经推送过，不用管
  3  发现没推送，已发出告警
  5  发现没推送，但告警也发不出去（webhook 失效等）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest import monitor  # noqa: E402
from digest.notifiers.wecom import (  # noqa: E402
    NotifierError,
    WeComError,
    WeComNotifier,
    mask_url,
)
from digest.state import already_pushed, load_state_doc  # noqa: E402

REPO = "palmer67/github-daily-digest"
ACTIONS_URL = f"https://github.com/{REPO}/actions/workflows/m5-daily.yml"
DEFAULT_STATE = Path("state/sent.json")


def build_alert(day: str, reason: str) -> str:
    """告警文案。只讲事实和下一步，不放任何密钥。"""
    return (
        f"## ⚠️ 每日开源速递没跑成（{day}）\n"
        f"到现在还没有推送记录。{reason}\n"
        "常见原因：定时任务没触发（cron 时区不对 / 被平台禁用）、"
        "或者任务跑失败了。\n"
        f"[👉 看运行记录]({ACTIONS_URL})"
    )


def check(day: str, state_path: Path = DEFAULT_STATE) -> tuple[bool, str]:
    """返回 (今天是否已推送, 说明)。"""
    if not state_path.exists():
        return False, "状态文件不存在"
    try:
        doc = load_state_doc(state_path)
    except Exception as exc:  # 坏文件也要告警，不能静默
        return False, f"状态文件读不出来（{type(exc).__name__}: {exc}）"
    if already_pushed(doc, day):
        run = doc.get("runs", {}).get(day, {})
        return True, (
            f"今天已经推过：{run.get('messages', '?')} 条消息、"
            f"{run.get('cards', '?')} 张卡"
        )
    return False, "state/sent.json 里今天还没有 pushed=true 的记录"


def _safe_stdout() -> None:
    """Windows 控制台是 GBK，打印 ⚠️ 会炸，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _safe_stdout()
    day = monitor.today_in_shanghai()
    dry_run = "--dry-run" in (argv if argv is not None else sys.argv[1:])

    pushed, detail = check(day, DEFAULT_STATE)
    if pushed:
        print(f"✅ {detail}（{day}），不用告警")
        return 0

    print(f"⚠️  {day} 没有推送记录：{detail}")
    message = build_alert(day, detail)
    print("--- 告警文案 ---")
    print(message)
    print("----------------")

    if dry_run:
        print("（--dry-run：不发送）")
        return 3

    url = os.environ.get("WECOM_WEBHOOK_URL", "")
    print(f"告警目标：{mask_url(url) if url else '（没有配 WECOM_WEBHOOK_URL）'}")
    try:
        WeComNotifier(url).send_markdown(message)
    except (NotifierError, WeComError) as exc:
        print(f"❌ 告警没发出去：{type(exc).__name__}: {exc}")
        return 5
    print("📣 告警已发到企业微信")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
