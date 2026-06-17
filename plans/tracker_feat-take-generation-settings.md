# Tracker: feat/take-generation-settings

Card #33 — "Store generation settings with each take" (https://trello.com/c/LGPuz0a2)

Two parts:
1. **Bug:** a take can lose its original settings when its shot is edited post-generation,
   so the exported `settings.txt` is incorrect. Export must use the take's immutable
   `settings_snapshot`, not the live shot.
2. **Feature:** in the take/video viewer, add a settings button (bottom-right, next to the
   frame timer) AND a right-click "Show generation settings" menu item; either opens a
   docked panel (right of the video) showing the take's original generation settings.

## Phase 1: Pick Up the Card
- [x] Claim the top card — two-phase handshake (move → claim comment → wait → earliest wins)
- [x] Pull latest main
- [x] Read the card
- [x] Create worktree (wt4) + branch + venv + push

## Phase 2: Research
- [x] Read export.py — already writes immutable snapshot; not the bug
- [x] Read store/models.py — Take.settings_snapshot shape
- [x] Read take_player.py (viewer) — frame_label is the "frame timer"; no menu/panel yet
- [x] Trace snapshot freeze (main_window.generate_shot) — missing canvas/crop
- [x] Summarized findings

## Phase 3: Design
- [x] Plan written + aligned with user (QDockWidget + complete the snapshot)

## Phase 4: Implement
- [x] Snapshot fix: add canvas + crop in generate_shot
- [x] Feature: ⚙ button + right-click menu + dockable settings panel + pure formatter
- [x] Update CLAUDE.md rule #3

## Phase 5: Verify
- [x] Run all 6 headless smoke phases (phase 6 needs real Fighter root)
- [x] Add smoke coverage (3 new tests in smoke_phase5)
- [ ] Manual UI smoke (settings panel) — flag for user
- [x] Spot-check diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch, resolve conflicts
- [ ] Re-run smoke suite
- [ ] PR + self-merge, fast-forward main
- [ ] Clean up worktree/branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Follow-up cards
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
