# Tracker: feat/overnight-batch-render

## Phase 1: Pick Up the Card
- [x] Create + claim card (6a326852, claim 53cb31ca), move to In Progress
- [x] Pull latest main
- [x] Create worktree (.trees/wt5) + branch + venv + push

## Phase 2: Research
- [x] Read generate_shot / confirm_launch / JobManager
- [ ] Confirm no existing per-shot take-count
- [ ] Confirm comfy_client.stop_server signature
- [ ] Windows sleep command

## Phase 3: Design
- [ ] Write plans/overnight-batch-render.md
- [ ] Align with user (done via AskUserQuestion: full scope, both backends, sleep PC max)

## Phase 4: Implement
- [ ] Refactor _queue_take helper out of generate_shot
- [ ] BatchRun controller (take-id set, post-action, start time, drain detection, report)
- [ ] JobManager drain detection / signal
- [ ] Batch dialog (which shots / N takes / when-done)
- [ ] Power commands (stop ComfyUI, sleep PC) - opt-in
- [ ] build_batch_report pure fn
- [ ] Wire "Generate batch..." action into Shots-tab control strip
- [ ] Update CLAUDE.md

## Phase 5: Verify
- [ ] Extend/add smoke phase (pure fns: report, eligibility, item builder, drain logic)
- [ ] Run all 6 smoke phases
- [ ] Manual UI smoke
- [ ] Spot-check diff

## Phase 6: Review & Ship
- [ ] Commit + push
- [ ] /review, fix findings
- [ ] Pull main into branch
- [ ] Re-run smoke
- [ ] PR + self-merge
- [ ] Clean up worktree/branch
- [ ] Delete plan + tracker
- [ ] Move card to Done + comment
- [ ] Overview to user
