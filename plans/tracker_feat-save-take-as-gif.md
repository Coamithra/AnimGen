# Tracker: feat/save-take-as-gif

Card: "Save take as animated GIF (disk or clipboard)" (6a33edf5, #70)

## Phase 1: Pick Up the Card
- [x] Claim the card — two-phase handshake (move to In Progress → claim comment fe3a3be4 → wait → earliest wins)
- [x] Pull latest main
- [x] Read the card
- [x] Create worktree (wt1) and branch (feat/save-take-as-gif), venv, push

## Phase 2: Research
- [ ] Read ui/take_player.py — context menu, frame decode, how frames/QImages are produced
- [ ] Read store/models.py Take — path to the .mp4 / source media
- [ ] Trace how the player loads + decodes frames (PyAV)
- [ ] Identify GIF encoding options (PyAV vs imageio vs PIL)
- [ ] Investigate Windows clipboard-as-animated-GIF feasibility
- [ ] Summarize findings

## Phase 3: Design
- [ ] Draft plan (plans/save-take-as-gif.md)
- [ ] Check reusable patterns
- [ ] Align with user — get approval before coding

## Phase 4: Implement
- [ ] Add "Save as GIF…" + "Copy GIF to clipboard" context menu entries
- [ ] GIF encode helper (pure, headless-testable)
- [ ] Wire up file dialog + clipboard
- [ ] Update CLAUDE.md if a documented contract changes

## Phase 5: Verify
- [ ] Add/extend smoke phase coverage
- [ ] Run all 7 smoke phases
- [ ] Manual UI smoke (control server / app launch)
- [ ] Spot-check diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch, resolve conflicts
- [ ] Re-run smoke suite
- [ ] PR + self-merge, fast-forward main
- [ ] Clean up worktree + branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Follow-up cards if needed
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances started
