"""Overnight batch render - pure planning + reporting helpers.

The UI (ui/main_window.start_batch / ui/batch_dialog) does the Qt work: gathering shots,
showing the single cost-confirm gate, enqueueing takes, and reacting to drain. Everything
that can be decided without Qt lives here so it's unit-testable headless (the crash_recovery
dependency-injection pattern): eligibility filtering, the cost-gate item list, the morning
report, and the OS suspend command.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED

# "When finished" power actions. STOP_COMFY frees the GPU; SLEEP also suspends the PC.
POWER_NONE = "none"
POWER_STOP_COMFY = "stop_comfy"
POWER_SLEEP = "sleep"

# Statuses that take a take out of the queue for good - used to know when a batch is done.
_TERMINAL = {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}


def is_terminal(status: str) -> bool:
    return status in _TERMINAL


@dataclass
class BatchPlan:
    items: list[dict]                       # confirm_launch item dicts, N per eligible shot
    eligible: list[tuple]                   # (shot, model, settings, est) per eligible shot
    skipped: list[tuple[str, str]]          # (shot_name, reason)
    takes_per_shot: int = 1

    @property
    def take_count(self) -> int:
        return len(self.eligible) * self.takes_per_shot


def plan_batch(shots, *, takes_per_shot: int,
               model_of: Callable[[object], Optional[dict]],
               aspects_of: Callable[[str], list],
               est_of: Callable[[str, dict], Optional[float]]) -> BatchPlan:
    """Decide which shots can generate and build the cost-gate item list (N per shot).

    A shot is skipped (with a reason) if its model is unknown, its chosen aspect isn't
    valid for that model, or it has no start keyframe. Mirrors the per-shot checks in
    MainWindow.generate_shot. The injected callables resolve model / valid aspects / cost
    so this stays free of library/project imports at call time and is headless-testable.
    """
    items: list[dict] = []
    eligible: list[tuple] = []
    skipped: list[tuple[str, str]] = []
    n = max(1, int(takes_per_shot))
    for shot in shots:
        model = model_of(shot)
        if not model:
            skipped.append((shot.name, f"unknown model: {shot.model_id}"))
            continue
        aspect = (shot.crop or {}).get("aspect")
        if aspect and aspect not in aspects_of(shot.model_id):
            skipped.append((shot.name, f"'{aspect}' not a valid aspect for {model['display_name']}"))
            continue
        if not shot.start_frame:
            skipped.append((shot.name, "no start keyframe"))
            continue
        settings = {**model.get("default_params", {}), **shot.settings}
        est = est_of(shot.model_id, settings)
        eligible.append((shot, model, settings, est))
        for _ in range(n):
            items.append({"name": shot.name, "model_display": model["display_name"],
                          "backend": model["backend"], "est_cost": est, "params": settings})
    return BatchPlan(items=items, eligible=eligible, skipped=skipped, takes_per_shot=n)


@dataclass
class BatchRun:
    """In-memory tracker for one in-flight batch (not persisted - a mid-batch app restart
    falls back to ordinary orphan recovery). Done when every take has reached a terminal
    status; that covers normal finish, workflow error, user cancel, and 3-strike abandon
    (which cancels its pending takes)."""
    take_ids: set
    power_action: str
    started: str                            # ISO timestamp, stamped by the caller
    remaining: set = field(default_factory=set)

    def __post_init__(self):
        if not self.remaining:
            self.remaining = set(self.take_ids)

    def mark(self, take_id: str, status: str) -> None:
        """Record a take's status; drop it from `remaining` once it's terminal."""
        if take_id in self.take_ids and is_terminal(status):
            self.remaining.discard(take_id)

    @property
    def complete(self) -> bool:
        return not self.remaining


def build_batch_report(rows: list[dict], *, started: str, finished: str,
                       power_action: str = POWER_NONE) -> str:
    """Plain-text morning summary. rows = [{name, status, cost_actual}]."""
    counts: dict[str, int] = {}
    total_cost = 0.0
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        c = r.get("cost_actual")
        if c:
            total_cost += c
    lines = ["AnimGen overnight batch report",
             f"Started:  {started}",
             f"Finished: {finished}",
             f"Takes:    {len(rows)}",
             "",
             "By status:"]
    for status in ("done", "failed", "cancelled"):
        if counts.get(status):
            lines.append(f"  {status:<10} {counts[status]}")
    for status, n in counts.items():       # any status not in the canonical three
        if status not in ("done", "failed", "cancelled"):
            lines.append(f"  {status:<10} {n}")
    lines += ["", f"Actual spend (where reported): ${total_cost:.2f}",
              f"Power action: {power_action}", "", "Takes:"]
    for r in rows:
        cost = f"  ${r['cost_actual']:.2f}" if r.get("cost_actual") else ""
        lines.append(f"  [{r['status']}] {r['name']}{cost}")
    return "\n".join(lines) + "\n"


def sleep_command() -> Optional[list[str]]:
    """The OS command to suspend (sleep) the machine, or None if unsupported here.

    Windows: SetSuspendState's first arg is Hibernate (0 = sleep); if the system has
    hibernation enabled it may hibernate instead - acceptable for an end-of-batch power
    action. Best-effort; the caller swallows failures.
    """
    if sys.platform == "win32":
        return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
    if sys.platform == "darwin":
        return ["pmset", "sleepnow"]
    return ["systemctl", "suspend"]
