# Tracker: fix/defer-keyposes-deletion

Card #56 (6a33ae6a): [Med] Defer keyposes-tree deletion until the flatten migration is persisted (store/project.py)

## Phase 1: Pick Up the Card
- [x] Claim the top card — two-phase handshake (claim f415c584 won, earliest at 08:41:26.255Z)
- [x] Pull latest main (already up to date)
- [x] Read the card (description + comments)
- [x] Create worktree and branch (wt3, fix/defer-keyposes-deletion)
- [ ] Set up venv in worktree
- [ ] Push branch upstream

## Phase 2: Research
- [ ] Read store/project.py _migrate_flatten_keyposes (lines 204-232) + load() (line 145)
- [ ] Understand how shot stars write-through works (reference pattern)
- [ ] Understand atomic-write + RLock discipline
- [ ] Read existing smoke_phase1.py legacy_path coverage (~line 248)

## Phase 3: Design
- [ ] Decide: (a) persist write-through before delete, or (b) defer delete to next save()
- [ ] Draft plan, align with user

## Phase 4: Implement
- [ ] Implement the chosen fix
- [ ] Update CLAUDE.md if a new persistence rule is introduced

## Phase 5: Verify
- [ ] Add migration round-trip assertion to smoke_phase1.py
- [ ] Run all 7 smoke phases
- [ ] Spot-check the diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review and fix findings
- [ ] Pull main into branch, resolve conflicts
- [ ] Re-run smoke suite
- [ ] PR + self-merge
- [ ] Clean up worktree/branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Follow-up cards if needed
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
