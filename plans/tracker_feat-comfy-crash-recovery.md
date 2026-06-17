# Tracker: feat/comfy-crash-recovery

Card: comfy crash detection (6a31ed57) — https://trello.com/c/OiOdcgC2

Detect ComfyUI mid-render crashes (GPU watchdog/TDR), restart the server, requeue
the failed take + the rest, surface "failed in XmYs, retrying" in the queue, and
abandon/pause the whole queue after a single take fails 3x.

## Phase 1: Pick Up the Card
- [x] Claim the top card (two-phase handshake — claim 9bec673f, won)
- [x] Pull latest main
- [x] Read the card
- [x] Create worktree wt2 + branch feat/comfy-crash-recovery
- [ ] venv set up in worktree

## Phase 2: Research
- [x] Read comfy_client.py (submit, server lifecycle, preflight, status)
- [x] Read jobs.py (JobManager, worker, signals, requeue surfaces)
- [x] Read queue_view.py (how status/notes are shown)
- [x] Read store/models.py + project.py (Job/Take state, retry fields)
- [x] Trace how a local render failure currently surfaces
- [x] Summarize findings + root cause

## Phase 3: Design
- [x] Draft plans/comfy-crash-recovery.md
- [x] Decide crash-detection signal + restart + requeue mechanics + 3-strike abandon
- [x] Identify pure functions to split out for smoke testing
- [x] Align with user, get approval

## Phase 4: Implement
- [x] Implement detection + recovery (crash_recovery.py, comfy_client restart/wait, jobs.abandon_local, main_window wiring)
- [x] Update CLAUDE.md (rule 12 + arch map rows)

## Phase 5: Verify
- [x] Run all 6 smoke phases (all PASS; phase6 needs ANIMGEN_FIGHTER_ROOT=C:/Programming/Fighter in worktree)
- [x] Add/extend smoke coverage (test_crash_recovery/wait_until_responsive/restart_server/abandon_local)
- [ ] Spot-check diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch
- [ ] Re-run smoke
- [ ] PR + self-merge
- [ ] Clean up worktree + branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Follow-up cards
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
