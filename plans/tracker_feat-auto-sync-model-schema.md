# Tracker: feat/auto-sync-model-schema

Card #18: Auto-sync model options from Replicate's live schema (id `6a3180fd`)

## Phase 1: Pick Up the Card
- [x] Claim the top card — two-phase handshake (claim 5806635e won, earlier than b99d9937)
- [x] Pull latest main (already up to date)
- [x] Read the card (description + comments)
- [x] Create worktree and branch (.trees/wt2, feat/auto-sync-model-schema)
- [x] Set up venv + push branch

## Phase 2: Research
- [ ] Read replicate_client.get_input_schema
- [ ] Read ui/shot_tab.py dropdown-building (enum or model.get(<field>))
- [ ] Read store/schema_cache.py + model_library.json structure
- [ ] Understand $ref/allOf/anyOf/oneOf resolution against components.schemas
- [ ] Find where startup happens (app.py) + View menu / Model Library tab refresh button
- [ ] Summarize findings

## Phase 3: Design
- [ ] Draft plans/auto-sync-model-schema.md
- [ ] Check reusable patterns
- [ ] Align with user — get approval before coding

## Phase 4: Implement
- [ ] Resolve $ref/allOf/anyOf/oneOf enums in get_input_schema
- [ ] Editor reads live enums first, authored lists as fallback
- [ ] Setting "update Replicate model data at startup" + View-settings menu
- [ ] Button in Model Library tab
- [ ] Update CLAUDE.md if conventions change

## Phase 5: Verify
- [ ] Run all six smoke phases
- [ ] Add/extend smoke coverage
- [ ] Manual UI smoke
- [ ] Spot-check diff
- [ ] Flag manual-test needs

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch
- [ ] Re-run smoke suite
- [ ] PR + self-merge
- [ ] Clean up worktree/branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Follow-up cards
- [ ] Overview to user

## Phase 7: Clean up
- [ ] Stop any app/ComfyUI instances
