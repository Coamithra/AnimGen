# Tracker: feat/claude-app-driver

## Phase 1: Pick Up the Card
- [x] Claim the top card (two-phase handshake, claim 2393bf72 — won)
- [x] Pull latest main
- [x] Read the card
- [x] Create worktree wt3 + branch feat/claude-app-driver

## Phase 2: Research
- [ ] Understand current app structure (app.py, MainWindow, widget objectNames)
- [ ] Survey options for driving a PySide6 app (computer-use MCP, embedded control server, QTest)
- [ ] Summarize findings

## Phase 3: Design
- [ ] Draft plan in plans/claude-app-driver.md
- [ ] Align with user (get approval before coding)

## Phase 4: Implement
- [ ] Build the chosen approach

## Phase 5: Verify
- [ ] Headless smoke suite (all 6)
- [ ] Manual smoke for the driver

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main, resolve conflicts
- [ ] Re-run smoke
- [ ] PR + self-merge
- [ ] Clean up worktree/branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
