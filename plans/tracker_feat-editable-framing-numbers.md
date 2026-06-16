# Tracker: feat/editable-framing-numbers

Card #12 — "Make position, size (even the percentage) numbers in the Framing panel editable for people who love that precise control" (6a31676c)

## Phase 1: Pick Up the Card
- [x] Claim the top card — two-phase handshake (move → claim comment d5383cbd → wait → earliest wins)
- [x] Pull latest main
- [x] Read the card (no description; title is the spec)
- [x] Create worktree wt3 + branch + venv + push

## Phase 2: Research
- [x] Read ui/placement_widget.py (the Framing panel readout)
- [x] Trace integration: shot_tab.py wires PlacementCanvas.changed
- [x] Found existing coverage: scripts/smoke_phase3.py test_placement_canvas

## Phase 3: Design
- [x] Draft approach (this file / response)
- [x] Align with user — center anchor, all six fields editable

## Phase 4: Implement
- [x] Replace read-only QLabels with editable spin boxes in _build_info_panel
- [x] Wire edits -> sprite pos/scale (center-anchored) -> emit changed
- [x] Guard refresh against feedback loops (_refreshing flag)
- [x] Update CLAUDE.md placement_widget row (stale "Size slider")

## Phase 5: Verify
- [x] Extend smoke_phase3 test_placement_canvas (edit fields -> placement updates + changed fires + no feedback loop)
- [x] Run full 6-phase smoke suite (all pass; phase6 needs ANIMGEN_FIGHTER_ROOT=C:/Programming/Fighter from a worktree)
- [x] Render panel to PNG + eyeball layout
- [x] Spot-check diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch
- [ ] Re-run smoke suite
- [ ] PR + self-merge, fast-forward main
- [ ] Clean up worktree/branch
- [ ] Delete tracker
- [ ] Move card to Done + comment
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
