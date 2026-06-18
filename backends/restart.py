"""Restart cancelled takes - pure planning helper (no Qt).

A cancelled take keeps its immutable settings_snapshot (rule #3), which - for takes made
on/after 2026-06-17 - carries the framing (canvas [w,h] + crop) alongside model/frames/
prompt/settings/seed. That's everything needed to re-fire the exact same render, so a
restart rebuilds the runner straight from the snapshot (same seed, same framing) and
re-runs the take IN PLACE.

A take that CAN'T be replayed exactly - its snapshot predates the framing-in-snapshot
change, its model has left the roster, or its start keyframe is gone - is reported here
with a reason; the caller marks it FAILED with that reason rather than re-generating it.

Everything decidable without Qt lives here so it's headless-testable (the batch.plan_batch
dependency-injection pattern): the injected callables resolve model / cost / path existence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from store.models import STATUS_CANCELLED, STATUS_FAILED


@dataclass
class RestartPlan:
    items: list[dict] = field(default_factory=list)          # confirm_launch items, one per restartable
    restartable: list = field(default_factory=list)          # takes replayable exactly from their snapshot
    unrestartable: list = field(default_factory=list)        # (take, reason) - caller marks these FAILED


def _has_framing(snap: dict) -> bool:
    """Whether the snapshot is the new (2026-06-17+) format that records framing.

    The framing keys (`canvas` + `crop`) were added together, so their presence is the
    discriminator for old vs new snapshots. A new-format take is replayable even with a
    [None, None] canvas (framing.render_keyposes defaults to the 1254 contract both times)."""
    return bool(snap) and "canvas" in snap and "crop" in snap


def plan_restart(takes, *, model_of_id: Callable[[Optional[str]], Optional[dict]],
                 est_of: Callable[[Optional[str], dict], Optional[float]],
                 path_exists: Callable[[Optional[str]], bool],
                 name_of: Callable[[object], str]) -> RestartPlan:
    """Split interrupted takes into exact-restartable vs unrestartable(+reason).

    Considers takes left in a terminal CANCELLED or FAILED state (a crash/app-death cancels a
    queued take, or fails an in-flight one whose render was lost) - the caller passes only the
    interrupted ones. A take is restartable iff its snapshot is new-format (has framing), its
    model is still in the roster, and its start keyframe still exists. The injected callables
    keep this free of library/project imports at call time so it's unit-testable headless.
    """
    plan = RestartPlan()
    for take in takes:
        if take.status not in (STATUS_CANCELLED, STATUS_FAILED):
            continue
        snap = take.settings_snapshot or {}
        label = name_of(take)
        if not _has_framing(snap):
            plan.unrestartable.append(
                (take, "snapshot predates framing-in-snapshot (2026-06-17); re-generate from the shot"))
            continue
        model = model_of_id(snap.get("model_id"))
        if not model:
            plan.unrestartable.append((take, f"unknown model: {snap.get('model_id')}"))
            continue
        start = snap.get("start_frame")
        if not path_exists(start):
            plan.unrestartable.append((take, "start keyframe no longer available"))
            continue
        end = snap.get("end_frame")
        if end and not path_exists(end):
            # render_keyposes silently drops a missing end frame, which would replay a
            # first-last take as start-only - not the exact render. Fail it instead.
            plan.unrestartable.append((take, "end keyframe no longer available"))
            continue
        settings = snap.get("settings") or {}
        plan.restartable.append(take)
        plan.items.append({
            "name": label, "model_display": model["display_name"],
            "backend": model["backend"], "est_cost": est_of(snap.get("model_id"), settings),
            "params": settings,
        })
    return plan
