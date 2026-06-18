# Tracker: feat/restart-cancelled-takes (card #49)

## Phase 1: Pick Up the Card
- [x] Claim the top card — two-phase handshake (won race, claim 1869c86b earliest)
- [x] Pull latest main
- [x] Read the card (description + DESIGN DECISION comment: snapshot-first, fresh fallback)
- [x] Create worktree (.trees/wt1) and branch (feat/restart-cancelled-takes), venv, push

## Phase 2: Research
- [x] Read main_window.generate_shot / _make_runner (current Generate -> runner build path)
- [x] Read backends/jobs.py (JobManager, cancel paths, re-enqueue surfaces)
- [x] Read store/project.py + store/models.py (Take, settings_snapshot, update_take, enumerate cancelled)
- [x] Read backends/batch.py plan_batch eligibility filter (mirror for restartable)
- [x] Read ui/main_window.py control strip (Cancel pending / Pause batch buttons)
- [x] Read ui/takes_view.py (context menu built inline w/ exec - needs _build_context_menu helper)
- [x] Read pipeline/framing.py render_keyposes (framing from snapshot vs shot)
- [x] Summarize findings (see plans/restart-cancelled-takes.md)

## Phase 3: Design
- [x] Draft plans/restart-cancelled-takes.md (snapshot-exact vs fresh fallback per-take)
- [x] Decide: snapshot-exact -> in place; fresh fallback -> new take (immutability forces this)
- [x] Align with user (approved: exact restart in place; else mark failed with message)

## Phase 4: Implement
- [x] Pure helper backends/restart.plan_restart (restartable vs unrestartable+reason)
- [x] jobs.restart_take (clears stale _cancelled/_stopping/_requeue, re-enqueues)
- [x] main_window: control-strip action + restart_cancelled_takes/_restart_takes/_restart_in_place/_shot_from_snapshot
- [x] Unrestartable -> mark FAILED with reason (no fresh fallback per user)
- [x] takes_view _build_context_menu (no exec) + Restart entry + restart_requested signal
- [x] Forward signal via shot_card + shot_tab; connect in main_window
- [x] Update CLAUDE.md (rule #17 + map rows)

## Phase 5: Verify
- [x] Add smoke coverage (test_restart_plan/_take/_from_snapshot in smoke_phase2)
- [x] Run all 7 smoke phases (PASS)
- [ ] Spot-check diff
- [ ] Flag manual-test items

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch, resolve conflicts
- [ ] Re-run smoke suite
- [ ] PR + self-merge, fast-forward main
- [ ] Clean up worktree + branch
- [ ] Delete plan + tracker files
- [ ] Move card to Done + comment
- [ ] Follow-up cards if needed
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
