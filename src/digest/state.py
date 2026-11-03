"""7.3 去重与补位。

核心是两个纯函数：
  apply_dedupe   —— 7 天（榜单栏目 2 天）内推过的项目剔掉
  backfill      —— 剔完不足 5 条时，从同一个查询的候选池里向下顺延补齐

状态文件读写放在文件末尾（M4 才会真正用起来），M1 只读不写。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence, TypeVar

T = TypeVar("T")


def parse_date(value: str | None) -> date | None:
    """把 '2026-11-01' 或 '2026-11-01T03:04:05Z' 解析成 date；解析不了返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text[: len("2026-11-01T03:04:05Z")], fmt).date()
        except ValueError:
            continue
    try:  # 兜底：ISO 8601
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def is_within_window(sent: date, today: date, days: int) -> bool:
    """sent 距今是否还在去重窗口内。

    days=7 表示「今天和往前 6 天」都算窗口内：昨天发过的要跳过，
    7 天前发过的可以重新出现（M4 验收：「超过 7 天后可再次出现」）。
    """
    delta = (today - sent).days
    return 0 <= delta < days


def apply_dedupe(
    items: Sequence[T],
    sent: dict[str, str],
    today: date,
    days: int,
    key=lambda item: getattr(item, "full_name"),
) -> tuple[list[T], list[T]]:
    """返回 (没推过的, 被去重掉的)。顺序保持不变。"""
    fresh: list[T] = []
    skipped: list[T] = []
    for item in items:
        last = parse_date(sent.get(key(item)))
        if last is not None and is_within_window(last, today, days):
            skipped.append(item)
        else:
            fresh.append(item)
    return fresh, skipped


def backfill(kept: Sequence[T], pool: Iterable[T], need: int) -> tuple[list[T], int]:
    """向下顺延补齐到 need 条。

    返回 (补齐后的列表, 缺口)。pool 是「同一查询里排在后面的候选」，
    绝不拿历史内容或别的栏目的内容凑数。缺口 > 0 时调用方要如实标注「今日仅 N 条」。
    """
    result = list(kept)
    for item in pool:
        if len(result) >= need:
            break
        result.append(item)
    shortfall = max(0, need - len(result))
    return result, shortfall


# ---------- 状态文件（M4 用，M1 只读） ----------

STATE_VERSION = 1


def load_state(path: str | Path) -> dict[str, str]:
    """读 state/sent.json，返回 {full_name: 'YYYY-MM-DD'}。文件不存在当空状态。"""
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"状态文件格式错误：{p} 顶层应为对象")
    sent = data.get("sent", data)
    if not isinstance(sent, dict):
        raise ValueError(f"状态文件格式错误：{p} 的 sent 字段应为对象")
    return {str(k): str(v) for k, v in sent.items()}


def dump_state(sent: dict[str, str]) -> str:
    return json.dumps(
        {"version": STATE_VERSION, "sent": dict(sorted(sent.items()))},
        ensure_ascii=False,
        indent=2,
    )


# ---------- 扩展状态（M4）：带每日运行记录，用来做幂等 ----------


def load_state_doc(path: str | Path) -> dict:
    """读完整状态文件：{"version":1,"sent":{...},"runs":{...}}。

    老文件只有 sent 也照读；文件坏掉直接抛错，不静默吞掉（否则去重会失效）。
    """
    p = Path(path)
    if not p.exists():
        return {"version": STATE_VERSION, "sent": {}, "runs": {}}
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"状态文件格式错误：{p} 顶层应为对象")
    sent = raw.get("sent", raw if "runs" not in raw else {})
    runs = raw.get("runs", {})
    if not isinstance(sent, dict) or not isinstance(runs, dict):
        raise ValueError(f"状态文件格式错误：{p} 的 sent / runs 应为对象")
    return {
        "version": int(raw.get("version", STATE_VERSION)),
        "sent": {str(k): str(v) for k, v in sent.items()},
        "runs": {str(k): dict(v) for k, v in runs.items()},
    }


def save_state_doc(path: str | Path, doc: dict) -> None:
    """原子写：先写临时文件再 replace，避免 Actions 中途被杀留下半个 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "sent": dict(sorted(doc.get("sent", {}).items())),
        "runs": {k: doc["runs"][k] for k in sorted(doc.get("runs", {}))},
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def completed_columns(doc: dict, day: str) -> set[str]:
    """返回当天确认推送成功的栏目，兼容旧的整次运行记录。"""
    run = doc.get("runs", {}).get(day) or {}
    columns = run.get("columns")
    if not isinstance(columns, dict):
        return set()

    completed: set[str] = set()
    for column_id, details in columns.items():
        if isinstance(details, dict) and details.get("pushed") is True:
            completed.add(str(column_id))
        elif run.get("pushed") and isinstance(details, dict) and "pushed" not in details:
            # Old records only had a run-wide flag, so every listed column succeeded.
            completed.add(str(column_id))
    return completed


def already_pushed(doc: dict, day: str, columns: Iterable[str] | None = None) -> bool:
    """检查整次运行或指定栏目是否已推送成功。"""
    run = doc.get("runs", {}).get(day) or {}
    if columns is None:
        return bool(run.get("pushed"))

    required = set(columns)
    if not required:
        return bool(run.get("pushed"))
    column_state = run.get("columns")
    if run.get("pushed") and (not isinstance(column_state, dict) or not column_state):
        # Legacy full-run record without per-column details.
        return True
    return required.issubset(completed_columns(doc, day))
