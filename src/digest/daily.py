"""M4：把采集 → 摘要 → 渲染 → 推送 → 写状态串成一次「每日速递」。

设计要点（对应策划书第 461–468 行 M4 验收）：
- 幂等：state 里记 `runs[YYYY-MM-DD].pushed`，同一天再跑直接秒退，不重复推送。
- 去重：`sent` 里存「项目 → 最后一次推送日期」，7 天内（榜单栏目 2 天）不再出现。
- 补位：去重后不足 items_per_column 时，用同一查询里排在后面的候选顺延补齐。
- 如实标注：顺延也补不齐，就在该栏目消息标题后写「今日仅 N 条」，绝不拿历史凑数。
- 状态落盘：推完立刻原子写 state/sent.json，由 workflow 负责 commit（M4 验收要求）。

这里不碰网络以外的副作用：推送、写文件都通过注入的对象完成，方便单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .config import Config, ConfigError, load_config
from .github import GitHubClient
from .readme import fetch_readme
from .renderer import render_messages
from .state import already_pushed, load_state_doc, save_state_doc
from .summarizer import SummaryResult, summarize
from .summarizer.deepseek import DeepSeekClient
from .pipeline import run_column


@dataclass
class ColumnRun:
    """一个栏目这一轮的结果，用于日志和状态记录。"""

    column_id: str
    column_name: str
    kept: int = 0
    target: int = 0
    shortfall: int = 0
    note: str = ""
    error: str | None = None
    cards_ok: int = 0
    cards_total: int = 0
    deduped: int = 0
    backfilled: int = 0
    seconds: float = 0.0

    @property
    def headline(self) -> str:
        if self.error:
            return f"{self.column_name}：采集失败（{self.error}）"
        extra = f"（去重 {self.deduped}、顺延补位 {self.backfilled}）" if (self.deduped or self.backfilled) else ""
        return f"{self.column_name}：{self.kept}/{self.target} 条{extra}{('　' + self.note) if self.note else ''}"


@dataclass
class DailyReport:
    """一次完整运行的结果。"""

    day: str
    skipped: bool = False
    skip_reason: str = ""
    columns: list[ColumnRun] = field(default_factory=list)
    messages: int = 0
    pushed: int = 0
    push_error: str | None = None
    state_path: str = ""
    state_written: bool = False
    seconds: float = 0.0
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        if self.skipped:
            return bool(self.skip_reason == "already_pushed")
        if self.push_error:
            return False
        return all(c.error is None for c in self.columns)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "dry_run": self.dry_run,
            "messages": self.messages,
            "pushed": self.pushed,
            "push_error": self.push_error,
            "state_path": self.state_path,
            "state_written": self.state_written,
            "seconds": round(self.seconds, 2),
            "columns": [
                {
                    "id": c.column_id,
                    "name": c.column_name,
                    "kept": c.kept,
                    "target": c.target,
                    "shortfall": c.shortfall,
                    "deduped": c.deduped,
                    "backfilled": c.backfilled,
                    "note": c.note,
                    "error": c.error,
                    "cards_ok": c.cards_ok,
                    "cards_total": c.cards_total,
                    "seconds": round(c.seconds, 2),
                }
                for c in self.columns
            ],
        }


def _shortfall_note(kept: int, target: int) -> str:
    """补不齐时的如实标注（M4 验收原文：消息里如实标注「今日仅 N 条」）。"""
    if kept < target:
        return f"今日仅 {kept} 条"
    return ""


def _pick_columns(config: Config, only: list[str] | None):
    """--columns 只跑指定栏目（验收或补跑用）；给了不存在的 id 就报错，别静默少跑。"""
    enabled = list(config.enabled_columns)
    if not only:
        return enabled
    known = {c.id: c for c in enabled}
    unknown = [cid for cid in only if cid not in known]
    if unknown:
        raise ConfigError(f"--columns 里有未知或未启用的栏目：{', '.join(unknown)}；可用的有 {', '.join(known)}")
    return [known[cid] for cid in only]


def build_columns(day: str, config: Config, gh: GitHubClient, sent: dict[str, str], only: list[str] | None = None):
    """采一批栏目，返回 (渲染用的 columns 结构, 每个栏目的运行记录, 备注字典)。"""
    rendered: list[tuple[str, str, list[tuple[Any, Any]]]] = []
    runs: list[ColumnRun] = []
    notes: dict[str, str] = {}

    for column in _pick_columns(config, only):
        need = config.items_per_column(column)
        started = time.time()
        result = run_column(gh, config, column, sent, date.fromisoformat(day))
        elapsed = time.time() - started

        run = ColumnRun(
            column_id=column.id,
            column_name=column.name,
            kept=len(result.items),
            target=need,
            shortfall=result.shortfall,
            note=_shortfall_note(len(result.items), need),
            error=result.error,
            deduped=int(result.stats.get("deduped", 0)),
            backfilled=int(result.stats.get("backfilled", 0)),
            seconds=elapsed,
        )
        runs.append(run)
        if result.error:
            continue
        if run.note:
            notes[column.id] = run.note
        rendered.append((column.id, column.name, list(result.items)))

    return rendered, runs, notes


def summarize_columns(
    rendered,
    gh: GitHubClient,
    client: DeepSeekClient,
    runs: list[ColumnRun],
) -> list[tuple[str, str, list[tuple[Any, Any]]]]:
    """给每条候选拉 README 并生成卡片；同一项目只总结一次（跨栏目复用）。"""
    by_name: dict[str, SummaryResult] = {}
    run_by_id = {r.column_id: r for r in runs}
    out: list[tuple[str, str, list[tuple[Any, Any]]]] = []

    for column_id, column_name, candidates in rendered:
        pairs: list[tuple[Any, Any]] = []
        run = run_by_id.get(column_id)
        for cand in candidates:
            cached = by_name.get(cand.full_name)
            if cached is None:
                text, _how = fetch_readme(gh, cand.full_name)
                cached = summarize(client, cand, text)
                by_name[cand.full_name] = cached
            pairs.append((cand, cached.card))
            if run is not None:
                run.cards_total += 1
                if cached.ok:
                    run.cards_ok += 1
        out.append((column_id, column_name, pairs))
    return out


def run_daily(
    config_path: str | Path,
    state_path: str | Path,
    day: str,
    *,
    gh: GitHubClient,
    client: DeepSeekClient,
    notifier: Any | None,
    columns: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> DailyReport:
    """跑一次每日速递。dry_run=True 时照常采集/摘要/渲染，但不推送、不写状态。"""
    report = DailyReport(day=day, dry_run=dry_run, state_path=str(state_path))
    started = time.time()

    if dry_run:
        log("⚠️ dry-run：只采集 + 摘要 + 渲染，不推送、不写状态")
        doc = load_state_doc(state_path)
    else:
        doc = load_state_doc(state_path)
    sent: dict[str, str] = dict(doc.get("sent", {}))

    if not force and not dry_run and already_pushed(doc, day):
        report.skipped = True
        report.skip_reason = "already_pushed"
        report.seconds = time.time() - started
        log(f"⏭️  {day} 已经推送过（runs 里有 pushed=true），本次直接退出，不重复推送")
        return report

    config = load_config(config_path)
    log(f"日期        : {day}")
    picked = _pick_columns(config, columns)
    log(f"栏目        : {len(picked)} 个（{'/'.join(c.id for c in picked)}）")
    log("-" * 64)

    rendered, runs, notes = build_columns(day, config, gh, sent, columns)
    for run in runs:
        log(("❌ " if run.error else "✅ ") + run.headline)

    if any(r.error for r in runs) and not any(r.kept for r in runs):
        report.columns = runs
        report.seconds = time.time() - started
        log("❌ 所有栏目都采集失败，不推送")
        return report

    cards = summarize_columns(rendered, gh, client, runs)
    total_ok = sum(r.cards_ok for r in runs)
    total_cards = sum(r.cards_total for r in runs)
    log(f"卡片        : {total_ok}/{total_cards} 张通过")

    result = render_messages(day, cards, notes=notes)
    report.messages = len(result.messages)
    log(f"消息        : {len(result.messages)} 条，共 {result.total_bytes:,} 字节")
    for warning in result.warnings:
        log(f"⚠️  {warning}")

    pushed = 0
    push_error = None
    if dry_run or not notifier:
        log("（dry-run 或没有 notifier，跳过推送）")
    else:
        for msg in result.messages:
            try:
                notifier.send_markdown(msg.text)
                pushed += 1
                log(f"📤 [{msg.column_name}] {msg.part}/{msg.parts} 段　{len(msg.text.encode())} 字节　errcode=0")
            except Exception as e:  # noqa: BLE001 - 推送失败要如实记录，不能吞
                push_error = str(e)
                log(f"❌ 推送失败，停在这里：[{msg.column_name}] {e}")
                break

    report.columns = runs
    report.pushed = pushed
    report.push_error = push_error

    if not dry_run:
        for _column_id, _name, pairs in cards:
            for cand, _card in pairs:
                sent[cand.full_name] = day
        doc["sent"] = sent
        doc.setdefault("runs", {})[day] = {
            "pushed": push_error is None and pushed == len(result.messages) and pushed > 0,
            "messages": pushed,
            "cards": total_ok,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "columns": {c.column_id: {"kept": c.kept, "target": c.target, "shortfall": c.shortfall} for c in runs},
        }
        save_state_doc(state_path, doc)
        report.state_written = True
        log(f"状态文件    : {state_path}（已写入 {len(sent)} 条去重记录）")

    report.seconds = time.time() - started
    log("-" * 64)
    if report.push_error:
        log(f"❌ 本轮失败：{report.push_error}")
    else:
        log(f"✅ 本轮完成：{pushed} 条消息、{total_ok} 张卡、{report.seconds:.1f}s")
    return report
