"""Orphan-take recovery for the local (ComfyUI) AND hosted (Replicate) backends.

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
workflow it echoes back from /history and /queue. The seed fallback is only sound when
the seed uniquely identifies a take: a fixed-seed shot (an authored, non-random seed)
gives all N takes the SAME seed, so a bare seed match would let a never-submitted PENDING
orphan misclaim a SIBLING's finished render (M5). `ambiguous_seeds()` finds those shared
`(shot_id, seed)` keys and the matchers refuse seed matching for them, leaving the take
FAIL/CANCEL (interrupted, restartable via rule #17) rather than reclaiming the wrong
video. `plan_comfy_recovery` is pure (it takes already-fetched, normalized history/queue
plus the ambiguity set) so it can be unit-tested headless; the UI layer fetches off-thread
and executes the returned plans.

A hosted (Replicate) take orphans differently: the prediction keeps running (and billing) on
Replicate's servers, so `replicate_orphans` + `plan_replicate_recovery` reconcile each stuck
`generating` take against `replicate_client.get_prediction` (an idempotent GET - it OBSERVES,
never re-runs or re-charges) - succeeded -> reclaim the paid-for output, failed/canceled ->
mark failed/cancelled, still-running/unverifiable/no-job-id -> mark FAILED + interrupted
(restartable) rather than leave a permanent, undismissable "running" row in the Queue.
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
    prompt_id: Optional[str] = None     # the matched ComfyUI prompt id / replicate prediction id
    output_path: Optional[str] = None   # absolute source file to copy in (comfy reclaim only)
    reason: str = ""
    prediction: Optional[dict] = None   # the succeeded replicate prediction to download from
                                        # (hosted reclaim only; carries the output URL)
    interrupted: Optional[bool] = None  # hosted plans set this explicitly (True = crash/lost/
                                        # unverifiable -> restartable; False = genuine render
                                        # failure). None on comfy plans (that executor sets it).


def comfy_orphans(project) -> list:
    """Takes that a prior session left mid-flight on the local backend.

    Only comfyui-backed takes (per the immutable settings_snapshot) - a hosted/replicate
    take orphans differently and isn't reconciled here. Generating-before-pending, then
    oldest-first, so seed claiming is deterministic.

    include_deleted=True: a take binned while still mid-flight is otherwise never reconciled
    (H2) - it sits `generating` forever and its ComfyUI render keeps running unclaimed. Its
    plan still runs (reclaim/fail a generating one, cancel a pending one); the take stays
    binned throughout (recovery never clears `deleted`)."""
    orphans = [t for t in project.list_takes(include_deleted=True)
               if t.status in (STATUS_GENERATING, "pending")
               and (t.settings_snapshot or {}).get("backend") == "comfyui"]
    orphans.sort(key=lambda t: (t.status != STATUS_GENERATING, t.created or ""))
    return orphans


def ambiguous_seeds(project) -> set:
    """`(shot_id, seed)` keys shared by >=2 takes of the same shot across the WHOLE project.

    A concrete seed only *identifies* a take's render when it's unique within its shot. A
    fixed-seed shot (an authored, non-random seed) gives all N takes the SAME seed, so a bare
    seed match against /history or /queue is ambiguous - a never-submitted PENDING orphan
    would misclaim a SIBLING's finished render (M5). We compute ambiguity over the full take
    population (every status, `include_deleted`), NOT just the orphan set, because the sibling
    an orphan would collide with is often a non-orphan (already-DONE) take not in that set.
    Assumes colliding siblings are still in the take list: a sibling hard-removed via
    `purge_takes` no longer counts, so its stale /history render could still be misclaimed
    (narrow window; requires a purge between the sibling finishing and the app restart)."""
    counts: dict = {}
    for t in project.list_takes(include_deleted=True):
        if t.seed is not None:
            counts[(t.shot_id, t.seed)] = counts.get((t.shot_id, t.seed), 0) + 1
    return {key for key, n in counts.items() if n > 1}


def _seed_is_ambiguous(orphan, ambiguous: set) -> bool:
    return orphan.seed is not None and (orphan.shot_id, orphan.seed) in ambiguous


def _match_history(orphan, history: list[dict], claimed: set, ambiguous: set) -> Optional[dict]:
    """A finished, unclaimed history entry for this take (prompt-id first, then seed).

    The seed fallback is refused for a fixed-seed shot (an ambiguous `(shot_id, seed)`): the
    seed no longer uniquely identifies the take, so a match would risk claiming a sibling's
    render. prompt-id (`backend_job_id`) matching stays authoritative and is unaffected."""
    if orphan.backend_job_id:
        for h in history:
            if (h["prompt_id"] == orphan.backend_job_id and h["prompt_id"] not in claimed
                    and h["ok"] and h["outputs"]):
                return h
    if orphan.seed is not None and not _seed_is_ambiguous(orphan, ambiguous):
        # history is oldest-first; prefer the newest matching render.
        for h in reversed(history):
            if (orphan.seed in h["seeds"] and h["prompt_id"] not in claimed
                    and h["ok"] and h["outputs"]):
                return h
    return None


def _match_queue(orphan, queue: list[dict], claimed: set, ambiguous: set) -> Optional[dict]:
    """A running/pending, unclaimed queue entry for this take.

    Same fixed-seed guard as `_match_history`: an ambiguous seed can't REATTACH to a live
    queue entry that may be a sibling's render."""
    if orphan.backend_job_id:
        for q in queue:
            if q["prompt_id"] == orphan.backend_job_id and q["prompt_id"] not in claimed:
                return q
    if orphan.seed is not None and not _seed_is_ambiguous(orphan, ambiguous):
        for q in queue:
            if orphan.seed in q["seeds"] and q["prompt_id"] not in claimed:
                return q
    return None


def plan_comfy_recovery(orphans: list, history: list[dict], queue: list[dict],
                        ambiguous: Optional[set] = None) -> list[RecoveryPlan]:
    """Decide what to do with each orphaned take. Pure: no I/O, no mutation.

    `orphans`   - Take-like objects (.id/.shot_id/.status/.seed/.backend_job_id).
    `history`   - comfy_client.history_view() output.
    `queue`     - comfy_client.queue_view() output.
    `ambiguous` - `(shot_id, seed)` keys shared by >=2 takes (from `ambiguous_seeds()`).
                  Seed-only matching is refused for these fixed-seed shots so an orphan
                  never misclaims a sibling's render (M5); the take is left FAIL/CANCEL
                  (interrupted, restartable via rule #17) instead. `None` -> no shot has
                  a shared seed, so seed matching is safe for every orphan.
    """
    ambiguous = ambiguous or set()
    plans: list[RecoveryPlan] = []
    claimed: set = set()
    for o in orphans:
        h = _match_history(o, history, claimed, ambiguous)
        if h:
            claimed.add(h["prompt_id"])
            plans.append(RecoveryPlan(
                o.id, o.shot_id, RECLAIM, h["prompt_id"], str(h["outputs"][-1]),
                f"reclaimed finished render {str(h['prompt_id'])[:8]}"))
            continue
        q = _match_queue(o, queue, claimed, ambiguous)
        if q:
            claimed.add(q["prompt_id"])
            plans.append(RecoveryPlan(
                o.id, o.shot_id, REATTACH, q["prompt_id"], None,
                f"still {q['state']} on ComfyUI ({str(q['prompt_id'])[:8]}); re-attaching"))
            continue
        # No prompt-id match, and any seed match was refused as ambiguous (fixed-seed shot):
        # note that so the user knows the take was left restartable rather than misclaimed.
        ambiguous_note = (" (fixed-seed shot: seed match too ambiguous to auto-reclaim)"
                          if not o.backend_job_id and _seed_is_ambiguous(o, ambiguous)
                          else "")
        if o.status == STATUS_GENERATING:
            plans.append(RecoveryPlan(
                o.id, o.shot_id, FAIL, None, None,
                "no matching ComfyUI render found (lost to app restart)" + ambiguous_note))
        else:  # pending - jobs.py sets 'generating' before submit, so this never reached the server
            plans.append(RecoveryPlan(
                o.id, o.shot_id, CANCEL, None, None,
                "queued but not submitted before restart; re-Generate to run it" + ambiguous_note))
    return plans


def replicate_orphans(project) -> list:
    """Takes a prior session left mid-flight on the HOSTED (Replicate) backend.

    On load there are no live workers, so every replicate-backed take still at
    `generating`/`pending` is orphaned. Unlike a local render, a hosted prediction keeps
    running (and billing) on Replicate's servers; left frozen at `generating` it shows a
    permanent, undismissable "running" row in the Queue and is never reconciled. This selects
    those takes so `plan_replicate_recovery` can reconcile each against its prediction.

    include_deleted=True for the same reason as `comfy_orphans` (H2): a take binned while
    mid-flight would otherwise never be reconciled. Generating-before-pending, oldest-first."""
    orphans = [t for t in project.list_takes(include_deleted=True)
               if t.status in (STATUS_GENERATING, "pending")
               and (t.settings_snapshot or {}).get("backend") == "replicate"]
    orphans.sort(key=lambda t: (t.status != STATUS_GENERATING, t.created or ""))
    return orphans


# Replicate prediction statuses (https://replicate.com/docs). "starting"/"processing" are the
# still-running states; the rest are terminal.
_REPLICATE_RUNNING = ("starting", "processing")


def plan_replicate_recovery(orphans: list, statuses: dict) -> list[RecoveryPlan]:
    """Decide what to do with each orphaned HOSTED take. Pure: no I/O, no mutation, no spend.

    `orphans`  - Take-like objects (.id/.shot_id/.status/.backend_job_id).
    `statuses` - {take_id: prediction_dict_or_None}. The prediction dict is
                 `replicate_client.get_prediction(backend_job_id)`'s result (its `status` is
                 starting/processing/succeeded/failed/canceled), or `None` when the fetch could
                 not be performed (no backend_job_id recorded, or the network was down / the
                 poll raised). A take id absent from the dict is treated as `None` too.

    Reconciliation NEVER creates spend - it only observes an already-running prediction, so:
      - succeeded            -> RECLAIM (download the output the session already paid for; if the
                                download itself can't complete this session the executor leaves the
                                take GENERATING so NEXT launch retries the free download - it never
                                re-bills an already-paid render)
      - failed               -> FAIL    (genuine render failure; interrupted=False)
      - canceled             -> CANCEL  (already stopped on the server; interrupted=True)
      - starting/processing  -> FAIL + interrupted (still billing on Replicate, but there's no
                                worker to re-attach to and re-attaching would mean a fresh poll
                                loop; mark it lost-to-restart and restartable rather than leave a
                                permanent zombie row)
      - unverifiable (no prediction id, OR Replicate unreachable / poll raised -> status None):
                                GENERATING -> FAIL + interrupted, PENDING -> CANCEL + interrupted
                                (a never-submitted PENDING take mirrors the comfy CANCEL; both
                                restartable). We can't tell a running from a done prediction here,
                                so we clear the frozen row rather than leave it "running" forever.

    Each plan carries an explicit `interrupted` flag (True = crash/lost/unverifiable, restartable
    via rule #17; False = genuine render failure) so the executor never has to re-derive intent -
    the bulk "Restart interrupted takes" picks up exactly the interrupted ones, like the comfy side."""
    plans: list[RecoveryPlan] = []
    for o in orphans:
        pred = statuses.get(o.id)
        status = pred.get("status") if pred else None
        if status == "succeeded":
            plans.append(RecoveryPlan(
                o.id, o.shot_id, RECLAIM, o.backend_job_id, None,
                f"reclaimed finished hosted render {str(o.backend_job_id or '')[:8]}",
                prediction=pred, interrupted=False))
        elif status == "failed":
            err = (pred.get("error") or "").strip()
            reason = "hosted render failed on Replicate" + (f": {err[:200]}" if err else "")
            plans.append(RecoveryPlan(o.id, o.shot_id, FAIL, o.backend_job_id, None, reason,
                                      prediction=pred, interrupted=False))
        elif status == "canceled":
            plans.append(RecoveryPlan(
                o.id, o.shot_id, CANCEL, o.backend_job_id, None,
                "hosted render was cancelled on Replicate", interrupted=True))
        elif status in _REPLICATE_RUNNING:
            plans.append(RecoveryPlan(
                o.id, o.shot_id, FAIL, o.backend_job_id, None,
                f"hosted render still {status} on Replicate at restart with no live worker; "
                "re-Generate to run it again", interrupted=True))
        elif pred is None:      # unverifiable: no prediction id, or the poll failed (network down)
            if o.status == STATUS_GENERATING:
                plans.append(RecoveryPlan(
                    o.id, o.shot_id, FAIL, o.backend_job_id, None,
                    "hosted render could not be verified on Replicate (no prediction id, or "
                    "Replicate was unreachable at restart); re-Generate to run it again",
                    interrupted=True))
            else:               # pending - never actually submitted, mirrors comfy CANCEL
                plans.append(RecoveryPlan(
                    o.id, o.shot_id, CANCEL, o.backend_job_id, None,
                    "hosted take was not submitted before restart (or Replicate was unreachable); "
                    "re-Generate to run it", interrupted=True))
        else:                   # an unrecognized status - can't classify, so clear + restartable
            plans.append(RecoveryPlan(
                o.id, o.shot_id, FAIL, o.backend_job_id, None,
                f"hosted render in an unrecognized Replicate state ({status!r}) at restart with "
                "no live worker; re-Generate to run it again", interrupted=True))
    return plans


def plan_offline_recovery(orphans: list) -> list[RecoveryPlan]:
    """Reconcile orphans when ComfyUI is UNREACHABLE (history/queue can't be fetched). Pure.

    With no server view we can't verify any render, and there are no live workers, so an
    orphan can't be making progress - a `generating` take is mislabeled "running" forever
    if it's left as-is and the server never comes back this session. So everything is
    cleared now rather than left a permanent zombie: a `generating` take -> FAIL (a render
    that was in flight when the app/server died and can't be confirmed), a `pending` one ->
    CANCEL (never reached the server). Re-Generate to run it again.
    """
    plans: list[RecoveryPlan] = []
    for o in orphans:
        if o.status == STATUS_GENERATING:
            plans.append(RecoveryPlan(
                o.id, o.shot_id, FAIL, None, None,
                "ComfyUI was unreachable at restart; render could not be recovered. "
                "Re-Generate to run it again."))
        else:  # pending - never reached the server
            plans.append(RecoveryPlan(
                o.id, o.shot_id, CANCEL, None, None,
                "not submitted before restart; re-Generate to run it"))
    return plans
