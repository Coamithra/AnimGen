"""Orphan-take recovery for the local (ComfyUI) backend.

A local render is polled by a worker thread *inside* AnimGen (comfy_client.submit's
loop). The ComfyUI server is a separate, surviving process: if AnimGen is closed or
crashes mid-render, that worker dies but the server keeps rendering and writes the
finished .mp4 into its output dir. The take is then left frozen at `generating`
forever, and the file sits unclaimed.

On project load there are, by definition, no live workers - so every take still at
`generating`/`pending` is orphaned. This module reconciles those against the server:

  - finished on the server   -> RECLAIM  (copy the output in, mark the take done)
  - still running/queued      -> REATTACH (re-poll it to completion)
  - gone (no trace)           -> FAIL     (generating) - lost to the restart
  - never submitted           -> CANCEL   (pending) - re-Generate to run it

Matching is by the take's recorded `backend_job_id` (the ComfyUI prompt id, persisted
at submit) and falls back to the take's concrete `seed`, which ComfyUI bakes into the
workflow it echoes back from /history and /queue. `plan_comfy_recovery` is pure (it
takes already-fetched, normalized history/queue) so it can be unit-tested headless; the
UI layer fetches off-thread and executes the returned plans.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from store.models import STATUS_GENERATING

# Plan actions.
RECLAIM = "reclaim"      # finished render found -> copy in + mark done
REATTACH = "reattach"    # still on the server -> re-poll to completion
FAIL = "fail"            # generating take with no server trace -> mark failed
CANCEL = "cancel"        # pending take never submitted -> mark cancelled


@dataclass
class RecoveryPlan:
    take_id: str
    shot_id: str
    action: str
    prompt_id: Optional[str] = None     # the matched ComfyUI prompt (reclaim/reattach)
    output_path: Optional[str] = None   # absolute source file to copy in (reclaim only)
    reason: str = ""


def comfy_orphans(project) -> list:
    """Takes that a prior session left mid-flight on the local backend.

    Only comfyui-backed takes (per the immutable settings_snapshot) - a hosted/replicate
    take orphans differently and isn't reconciled here. Generating-before-pending, then
    oldest-first, so seed claiming is deterministic."""
    orphans = [t for t in project.list_takes(include_deleted=False)
               if t.status in (STATUS_GENERATING, "pending")
               and (t.settings_snapshot or {}).get("backend") == "comfyui"]
    orphans.sort(key=lambda t: (t.status != STATUS_GENERATING, t.created or ""))
    return orphans


def _match_history(orphan, history: list[dict], claimed: set) -> Optional[dict]:
    """A finished, unclaimed history entry for this take (prompt-id first, then seed)."""
    if orphan.backend_job_id:
        for h in history:
            if (h["prompt_id"] == orphan.backend_job_id and h["prompt_id"] not in claimed
                    and h["ok"] and h["outputs"]):
                return h
    if orphan.seed is not None:
        # history is oldest-first; prefer the newest matching render.
        for h in reversed(history):
            if (orphan.seed in h["seeds"] and h["prompt_id"] not in claimed
                    and h["ok"] and h["outputs"]):
                return h
    return None


def _match_queue(orphan, queue: list[dict], claimed: set) -> Optional[dict]:
    """A running/pending, unclaimed queue entry for this take."""
    if orphan.backend_job_id:
        for q in queue:
            if q["prompt_id"] == orphan.backend_job_id and q["prompt_id"] not in claimed:
                return q
    if orphan.seed is not None:
        for q in queue:
            if orphan.seed in q["seeds"] and q["prompt_id"] not in claimed:
                return q
    return None


def plan_comfy_recovery(orphans: list, history: list[dict],
                        queue: list[dict]) -> list[RecoveryPlan]:
    """Decide what to do with each orphaned take. Pure: no I/O, no mutation.

    `orphans`  - Take-like objects (.id/.shot_id/.status/.seed/.backend_job_id).
    `history`  - comfy_client.history_view() output.
    `queue`    - comfy_client.queue_view() output.
    """
    plans: list[RecoveryPlan] = []
    claimed: set = set()
    for o in orphans:
        h = _match_history(o, history, claimed)
        if h:
            claimed.add(h["prompt_id"])
            plans.append(RecoveryPlan(
                o.id, o.shot_id, RECLAIM, h["prompt_id"], str(h["outputs"][-1]),
                f"reclaimed finished render {str(h['prompt_id'])[:8]}"))
            continue
        q = _match_queue(o, queue, claimed)
        if q:
            claimed.add(q["prompt_id"])
            plans.append(RecoveryPlan(
                o.id, o.shot_id, REATTACH, q["prompt_id"], None,
                f"still {q['state']} on ComfyUI ({str(q['prompt_id'])[:8]}); re-attaching"))
            continue
        if o.status == STATUS_GENERATING:
            plans.append(RecoveryPlan(
                o.id, o.shot_id, FAIL, None, None,
                "no matching ComfyUI render found (lost to app restart)"))
        else:  # pending - jobs.py sets 'generating' before submit, so this never reached the server
            plans.append(RecoveryPlan(
                o.id, o.shot_id, CANCEL, None, None,
                "queued but not submitted before restart; re-Generate to run it"))
    return plans
