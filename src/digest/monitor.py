"""M5：告警与心跳。

三件事：
  write_heartbeat —— 推送确实成功后，把「今天几点推了几条」落到 state/heartbeat.json 并提交
  ping            —— 可选的对外心跳（HEARTBEAT_URL，例如 healthchecks.io），只在真成功后调用
  read/missing    —— 备用 cron 用来看「今天到点还没心跳」→ 发告警

心跳的意义：企业微信本身挂了、或者 webhook 失效时，告警发不出去，
外部心跳服务会在约定时间没收到 ping 时给你发邮件——这是最后一道兜底。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

HEARTBEAT_FILE = "heartbeat.json"


def heartbeat_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / HEARTBEAT_FILE


def write_heartbeat(state_dir: str | Path, day: str, messages: int, cards: int, degraded: bool = False) -> Path:
    """推送成功后写心跳文件（跟着 state 一起被 commit，60 天无活动被停用的风险也靠它兜）。"""
    path = heartbeat_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": day,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "messages": messages,
        "cards": cards,
        "degraded": degraded,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_heartbeat(state_dir: str | Path) -> dict:
    path = heartbeat_path(state_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def heartbeat_missing(state_dir: str | Path, day: str) -> bool:
    """今天的心跳是不是还没到（备用 cron 与告警用）。"""
    return read_heartbeat(state_dir).get("date") != day


def heartbeat_url() -> str:
    return os.environ.get("HEARTBEAT_URL", "").strip()


def ping(url: str = "", timeout: float = 10.0) -> tuple[bool, str]:
    """向外部心跳服务报平安。没配就当跳过（返回 ok=True，detail=skipped）。"""
    target = (url or heartbeat_url()).strip()
    if not target:
        return True, "skipped: 没有配 HEARTBEAT_URL"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:  # noqa: S310 - 只打配置里的固定地址
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


def alert_message(day: str, reason: str, detail: str = "") -> str:
    """告警文案。故意不带任何 key / URL。"""
    lines = [
        f"## ⚠️ 每日开源速递告警（{day}）",
        "",
        f"**原因**：{reason}",
    ]
    if detail:
        lines.append(f"**细节**：{detail}")
    lines += [
        "",
        "处理建议：看 GitHub Actions 最近一次运行日志；修好后可以在 Actions 里手动 `Run workflow` 补发。",
    ]
    return "\n".join(lines)


def stale_alert(day: str, hb: dict) -> str:
    """备用 cron 发现「今天还没成功推送」时的告警。"""
    if not hb:
        detail = "state/heartbeat.json 里还没有任何记录"
    else:
        detail = f"最近一次心跳是 {hb.get('date')}（{hb.get('at')}），不是今天"
    return alert_message(day, "到点没有收到成功心跳，今天的速递可能没推出去", detail)


def today_in_shanghai() -> str:
    """Actions runner 是 UTC，这里按 +08:00 取日期，和 cron timezone 保持一致。"""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


__all__ = [
    "HEARTBEAT_FILE",
    "alert_message",
    "heartbeat_missing",
    "heartbeat_path",
    "heartbeat_url",
    "ping",
    "read_heartbeat",
    "stale_alert",
    "today_in_shanghai",
    "write_heartbeat",
]
