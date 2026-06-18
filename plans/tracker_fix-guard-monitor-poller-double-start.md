# Tracker: fix/guard-monitor-poller-double-start

Card 6a33ae76 — [Med-Low] Guard `_MonitorPoller.start()` against leaking a second
concurrent poller on rapid tab toggling (ui/comfy_monitor_window.py)

## Phase 1: Pick Up the Card
- [x] Claim the top card (two-phase handshake; won card 6a33ae76 after losing 2 races)
- [x] Pull latest main (already up to date)
- [x] Read the card
- [x] Create worktree (.trees/wt8) and branch (fix/guard-monitor-poller-double-start)
- [x] Push branch

## Phase 2: Research
- [x] Read ui/comfy_monitor_window.py (_MonitorPoller, start/stop/_run, host wiring)
- [x] Confirm _stop has no external refs (grep — only inside _MonitorPoller)
- [x] Find call sites: main_window.py 1301/1303/1464/1519 (tab-visibility driven)
- [x] Confirm no smoke suite covers _MonitorPoller yet

## Phase 3: Design
- [x] Generation-token approach (replace _stop bool); retain self._thread handle
- [x] Drop in-flight snapshot when superseded (no stale/duplicate emits)
- [x] Test home: smoke_phase7.py (has QApplication setup)

## Phase 4: Implement
- [x] Rewrite _MonitorPoller (generation token, thread handle, supersede-on-start)
- [x] Add test_monitor_poller_supersede to smoke_phase7.py
- [x] CLAUDE.md: no update needed (internal mechanism; documented contract unchanged)

## Phase 5: Verify
- [x] Run all 7 smoke phases (headless) — all PASS
- [x] Spot-check the diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch, resolve conflicts
- [ ] Re-run smoke suite
- [ ] PR + self-merge, fast-forward main
- [ ] Clean up worktree/branch
- [ ] Delete tracker
- [ ] Move card to Done + comment
- [ ] Final overview to user
