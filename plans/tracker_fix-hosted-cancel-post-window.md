# Tracker: fix/hosted-cancel-post-window

Card 6a31f054 — "Tighten hosted-take cancel during the create-POST window"
(follow-up from card 6a31a03f / PR #26)

## Phase 1: Pick Up the Card
- [x] Claim the card (two-phase handshake — won with claim cf4e4212)
- [x] Pull latest main
- [x] Read the card
- [x] Create worktree wt1 + branch + venv + push

## Phase 2: Research
- [x] Read backends/jobs.py request_stop + worker run path
- [x] Read backends/replicate_client.py on_submit / cancel_prediction flow
- [x] Trace the _stopping flag lifecycle
- [x] Confirm the race window + summarize findings

## Phase 3: Design
- [x] Draft approach: worker re-checks _stopping right after on_submit, self-cancels
- [x] Identify smoke coverage

## Phase 4: Implement
- [x] Make the change (jobs.is_stop_requested + main_window on_submit self-cancel)
- [x] Update CLAUDE.md jobs.py row

## Phase 5: Verify
- [x] Run all six smoke phases (PASS; phase 6 needs ANIMGEN_FIGHTER_ROOT in worktree)
- [x] Add/extend smoke coverage (test_is_stop_requested + test_stop_during_submit_window)
- [x] Spot-check diff

## Phase 6: Review & Ship
- [x] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch
- [ ] Re-run smoke
- [ ] PR + self-merge + fast-forward main
- [ ] Clean up worktree/branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Follow-up cards if needed
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
