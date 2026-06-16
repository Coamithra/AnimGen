# Contributing: Tackling a Trello Card

Step-by-step workflow for picking up and completing any card from the [Animation Generator Tool Trello board](https://trello.com/b/7SycR6UZ) (board id `6a2d752eee5f9d7478ad3250`). Lists are **To Do → In Progress → Done** (plus a **Notes / Decisions** list).

AnimGen is a native **PySide6 desktop app** (Python 3.12) — there is no build step, no dev server, and no browser preview. The gate before shipping is the **headless smoke suite** (`scripts/smoke_phase1-6.py`), not a typecheck/`npm test`.

---

## Quick ship (no card / small change)

Not every change is a Trello card. For a quick fix or doc tweak that doesn't warrant the full runbook below, the default ship flow is **PR + auto self-merge**:

```
git checkout -b <prefix>/<short-name>     # off main
git add <files> && git commit -m "..."     # only the files you touched
git push -u origin <branch>
gh pr create --fill                        # PR record + URL, no clicking
gh pr merge --merge                        # self-merge (see note); use --merge, not --squash
git checkout main && git pull origin main  # fast-forward local main to the merge
```

**No approval needed.** `main` is an unprotected branch on this solo private repo, so GitHub disabling the "Approve" button on your *own* PR is irrelevant — a required review only applies under a branch-protection rule, and this repo has none. Don't stop to ask the user to approve or open the PR by hand. (If the user says "just merge / direct", skip the PR entirely and fast-forward `main`.) The full card runbook (Phase 6) uses this same merge step inside the worktree flow.

---

## Before You Start: Create a Tracker Doc

**This is mandatory.** Before doing anything else, create a file `plans/tracker_<branch>.md` (create the `plans/` directory if it doesn't exist yet) with every step from this runbook as a checkbox list. Example:

```markdown
# Tracker: fix/some-bug

## Phase 1: Pick Up the Card
- [ ] Claim the top card — two-phase handshake FIRST (move to In Progress → claim comment → wait 10s → earliest comment wins), before anything else
- [ ] Pull latest main
- [ ] Read the card (description, comments, linked plan)
- [ ] Create worktree and branch

## Phase 2: Research
- [ ] Read the referenced code
- [ ] Trace the call chain
...
```

Check off each step as you complete it. This is your source of truth for progress — if you get interrupted or context is lost, the tracker tells you exactly where you left off. Delete the tracker file after the card is shipped.

---

## Worktree Quick Reference

All work happens in an isolated **git worktree** under `.trees/` (gitignored). This lets multiple agents work on different cards simultaneously without interfering with each other. The root checkout stays on `main` — never switch it to a feature branch.

| Command | What it does |
|---------|-------------|
| `git worktree add .trees/wt<k> -b <branch> main` | Create a worktree in slot `wt<k>` + branch from main |
| `git worktree list` | Show all active worktrees |
| `git worktree remove .trees/<name>` | Remove a worktree (clean up) |
| `git worktree prune` | Clean up stale worktree references |

**Key rules:**
- Each worktree gets its own branch; a branch can only be checked out in one worktree at a time
- Gitignored files do NOT exist in a fresh worktree — most importantly `.venv/` and `data/` (the runtime `*.animproj` projects + their `.assets/` sidecars). Set up a venv in the worktree before running anything: `python -m venv .venv` then `.venv/Scripts/python.exe -m pip install -r requirements.txt`. If the card needs the starter project, re-seed it (see "Run / test / seed" in `CLAUDE.md`)
- All worktree directories live under `.trees/` (gitignored at repo root)
- Windows note: if `git worktree remove` fails with "Permission denied", `cd` out of the worktree first, kill any `python.exe` still running from that worktree's `.venv` (e.g. an app instance or a launched ComfyUI server), then retry. A freshly `uv`/`pip`-synced `.venv` being scanned by Defender/Search indexer can also hold a transient lock — retry after a few seconds
- **Slot naming (mandatory):** worktree directories use fixed slot names `wt1`..`wt8`, NOT branch names. Pick the lowest slot not shown in `git worktree list`. If `git worktree add` fails because the directory already exists, another agent grabbed that slot in the same instant — take the next one. Branch names stay fully descriptive; the slot is only the folder, and `git worktree list` shows which branch lives in which slot

### Running the app from a worktree

There is no dev server or port to manage — AnimGen is a desktop Qt app. Two ways to run it:

- **Launch the GUI:** `.venv/Scripts/python.exe app.py` from inside the worktree. It opens the last/seeded/new project. Only do this when you actually need to see the UI; a launched window holds a handle on the worktree dir (kill it before `git worktree remove`).
- **Headless smoke checks (the normal verification path):** `QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/smoke_phase<n>.py`. These spend no money, use no GPU, and never open a window — they're safe to run in any worktree in parallel.

Two cautions specific to this app:
- **Never call a modal's `.exec()` in a headless context** — it blocks forever with no display. Pure/`build_summary`-style functions are split out precisely so logic can be smoke-tested without a modal.
- **A live hosted (Replicate) or local (ComfyUI) take spends money / GPU time.** Don't fire one to "verify" a change unless the user has explicitly given the go-ahead for that card. The backends are verified offline.

---

## Phase 1: Pick Up the Card

> **Claim the card FIRST — and confirm the claim before you trust it.** When several agents are launched in tandem and each is told to "pick up the top card of To Do", they all read the board, go off and do some work, and only *then* move the card — so they all grab the *same* card. Moving a card to In Progress is a fast claim, but "read the board" and "move the card" can't be truly atomic, so two agents can *both* land on the same card within the same second. The fix is a two-phase claim: move it to In Progress immediately (the fast grab), then post a claim comment and wait — the **earliest claim comment wins**, deterministically, because comments carry server timestamps. Do the move *before* reading the card, pulling main, or any other step; keep the read→move gap to those two back-to-back commands with **nothing in between**. If the board shows the top card is already in In Progress (another agent beat you to it), claim the next To Do card down instead.

1. **Claim the top card with the two-phase handshake (do this first, nothing before it)** — Run these in order, with nothing else interleaved:
    1. **Mint a claim ID** once for this session — a short unique token (e.g. `python -c "import secrets; print(secrets.token_hex(4))"`). Reuse the same ID for every claim attempt this session.
    2. **Grab it** — View the To Do list (`trello --board 6a2d752eee5f9d7478ad3250 card ls "To Do"`), then *immediately* `trello --board 6a2d752eee5f9d7478ad3250 card move <card_id> "In Progress"` for the top card. (If the top card is already in In Progress, target the next one down instead.)
    3. **Post the claim comment** — `trello --board 6a2d752eee5f9d7478ad3250 comment add <card_id> "I am doing this now — claim <claim_id>"`. This exact phrase is the lock marker other agents scan for.
    4. **Wait 10-30s**, randomly pick a waiting length between these values, then **re-read the card's comments with their timestamps** — `trello --board 6a2d752eee5f9d7478ad3250 --json comment ls <card_id>`. Use `--json`: the formatted `comment ls` prints only the day, but the JSON `date` field is a millisecond-precision ISO timestamp, which is what a 10s tie-break needs.
    5. **Resolve ties — earliest claim comment wins.** Look at every comment containing "I am doing this now". If any such comment from a *different* agent (different claim ID) has a `date` **earlier than yours**, you lost the race: that agent owns the card. Back off — `trello --board 6a2d752eee5f9d7478ad3250 comment delete <card_id> <your_comment_id>` to remove your own claim comment (note it takes **both** the card id and the comment id, the `id` field from the JSON above), and **leave the card in In Progress** (don't yank it from the winner). Then:
        - If you were told to work a **specific** card, stop here — end the session; the card is taken.
        - If the request was **generic** ("top card of To Do"), go back to (ii) and claim the **next** To Do card down, repeating the whole handshake.
    6. **You hold the lock** when your claim comment is the earliest (or the only) "I am doing this now". Only now read the card and proceed.
2. **Pull latest main** — `git pull origin main` so you start from the newest code
3. **Read the card** — Now that it's claimed, read the card description and any linked spec under `plans/<file>.md`. The plan is the long-form source of truth; the card is a pointer
4. **Create worktree and branch** — Branch off `main` with a descriptive prefix:
    - Bugs: `fix/<short-name>` (e.g. `fix/take-persistence-race`)
    - Features: `feat/<short-name>` (e.g. `feat/batch-export`)
    - Refactoring: `refactor/<short-name>`
    - Docs / plans only: `docs/<short-name>`
    ```
    git worktree add .trees/wt<k> -b <branch> main   # lowest free slot; see Worktree Quick Reference
    cd .trees/wt<k>
    python -m venv .venv
    .venv/Scripts/python.exe -m pip install -r requirements.txt
    git push -u origin <branch>
    ```
5. **All subsequent work happens inside `.trees/wt<k>/`**

## Phase 2: Research

Dig into the problem before proposing solutions. Use `/research` for topics that need external context (e.g. Replicate model schemas, ComfyUI node/API behaviour, PySide6/Qt API quirks, PyAV frame extraction, the ComfyUI GPU-watchdog/dynamic-VRAM crash).

6. **Read the referenced code** — Card descriptions and `plans/*.md` cite specific files. Read them — descriptions can drift. The architecture map in `CLAUDE.md` is the fastest orientation
7. **Trace the call chain** — The layers (all documented in `CLAUDE.md`):
    - `store/project.py` / `store/models.py` — the file-based **Project** document (Shots / Takes / Jobs); the source of truth for persistence
    - `backends/replicate_client.py` (hosted) and `backends/comfy_client.py` (local ComfyUI + its server lifecycle/preflight) — the generation backends
    - `backends/jobs.py` — `JobManager` on `QThreadPool`; worker threads write takes back through `project.update_take`
    - `pipeline/framing.py` (`normalize_keypose`, `canvas_size`, `render_keyposes`), `pipeline/extract.py` (PyAV), `pipeline/export.py`, `pipeline/takes_io.py` (bin/restore)
    - `ui/` — the view layer (tabbed central widget: Shots / Assets / Model Library / ComfyUI Status; shot tab editor; placement canvas; cost-confirm gate). UI reads the project; it doesn't own persistence
8. **Identify the blast radius** — Does it touch the **`.animproj` / `takes.json` schema or persistence**? Project JSON writes are **write-through off worker threads** and must stay serialized under the `RLock` with atomic unique-temp writes — two takes finishing at once otherwise race `os.replace` on Windows (`WinError 32`). Does it touch the **framing pipeline** (canvas sizes, `normalize_keypose`, placement params)? The **cost-confirm gate** (must fire before every launch)? The **immutable `settings_snapshot`** frozen on each take at launch? The **local backend preflight** (`--disable-dynamic-vram` refusal)? UI-only changes have a much smaller blast radius — keep them UI-only
9. **Research unknowns** — Use `/research` for anything that needs external knowledge: Replicate prediction/schema details, ComfyUI workflow/node-role mapping, Qt threading/signal pitfalls, the GPU watchdog (TDR) behaviour behind the dynamic-VRAM rule
10. **Summarize findings** — Brief writeup of what you learned: root cause (bugs), design options (features), or risk areas (refactors). Becomes input to the design phase

## Phase 3: Design

11. **Draft the approach** — Either update the existing `plans/<file>.md` or write one. Include:
    - **Context**: what the card is about and why it matters
    - **Design**: file-by-file changes; any new fields on the `Shot`/`Take`/`Job` dataclasses (and their JSON-schema / migration implications); new backend params; new UI surfaces
    - **Tests**: which `scripts/smoke_phase*.py` suite gets new coverage (or whether a new phase is warranted), plus any pure functions to split out so they're testable without a modal
    - **Out of scope**: what you're explicitly *not* doing
12. **Check for reusable patterns** — Look for existing utilities and conventions before inventing new ones (e.g. the atomic-write helper `store.project._atomic_write_json`, the hybrid persistence split — shots buffer/`dirty`, takes write through — `library.aspect_ratios()`, the existing tab/`_AsyncCall`/off-thread-poller patterns in the ComfyUI Status tab)
13. **Align with the user** — Present the plan, get approval before writing code

## Phase 4: Implement

14. **Make the changes** — Edit files per the approved plan. Follow project conventions:
    - **PySide6 + Python 3.12.** Use `python` (NOT `python3` — it hits the Windows Store alias); always set `PYTHONIOENCODING=utf-8` (Windows defaults to cp1252 and crashes on UTF-8 in JSON/HTML). Pass Windows-style paths (`C:/...`) to `sys.path.insert`, not MINGW (`/c/...`) paths
    - **Cost-confirm gate before EVERY launch** (hosted or local). The dialog defaults to Cancel — don't bypass it
    - **Additive — copy in, never move external originals.** Importing a keyframe asset COPIES it into `.assets/`; delete-to-bin only moves files *under* the project's `.assets/`. Never relocate or delete anything outside the project (e.g. seeded `../Fighter/out/` references stay in place)
    - **Each take's `settings_snapshot` is immutable** — frozen at launch. Don't mutate it
    - **Serialize all project JSON writes under the `RLock`** and use unique temp names (the atomic-write helper holds the lock across build+write and retries for AV/indexer locks). Take persistence runs off worker threads
    - **Local backend MUST run with `--disable-dynamic-vram`** (GPU watchdog / TDR crash on the 12GB card). AnimGen doesn't start ComfyUI, so `comfy_client.preflight()` refuses to submit a local job if dynamic VRAM is on. Don't weaken that gate (escape hatch: `ANIMGEN_ALLOW_DYNAMIC_VRAM=1`)
    - **`model_library.json` is authored, not generated** — Replicate IDs/fields were verified via live schema fetch; don't auto-rewrite it
    - **Secrets:** never copy `.env` into the repo tree (this is a public repo). The runtime token fallback (env → repo `.env` → source-project `.env`) exists so you don't have to
    - **Comments**: default to none; only add when the *why* is non-obvious. Don't narrate what the code does
15. **Document new conventions** — Update `CLAUDE.md` if the change introduces new persistence rules, a schema/migration, new backend behaviour, new env vars, new scripts, or modifies a documented contract. `CLAUDE.md` is the pickup guide and the source of truth — keep the architecture map and "Hard-won rules" current

## Phase 5: Verify

There is no typecheck/build gate (Pyright flags `from store…/from ui…` as unresolved — false positives; the repo root is on `sys.path` at runtime). The smoke suite is the gate.

16. **Run the headless smoke suite** — all six phases must pass:
    ```
    for n in 1 2 3 4 5 6; do QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
      .venv/Scripts/python.exe scripts/smoke_phase$n.py; done
    ```
    Add or extend a smoke phase for the behaviour you changed. Smoke tests must stay headless — never call a modal's `.exec()`, and override `paths.SCRATCH_DIR` to a tempdir so untitled-project scratch stays out of `data/`
17. **Manual smoke for UI / pipeline changes** — smoke tests don't cover everything:
    - UI changes: launch `.venv/Scripts/python.exe app.py`, exercise the affected tab/dialog by hand
    - Persistence changes: open/save/Save-As a project, generate (or simulate) a take, reopen, confirm the `.animproj` + `takes.json` round-trip cleanly (watch for `WinError 32` under concurrent take writes)
    - Framing changes: eyeball the placement canvas across wide/tall aspects and both backends' canvas sizes
    - Backend changes: a **live hosted or local take spends money / GPU — only with the user's explicit go-ahead.** Otherwise verify offline (preflight, request-build, schema) as the existing tests do
    - Document the steps in the plan's "Verification" section
18. **Spot-check the diff** — Read through once more for typos, dict keys that don't exist, mutated snapshots, project writes that escaped the lock, a launch path that skips the cost gate, and dead-code residue
19. **Flag what needs manual testing** — Leave a note for the user of anything that can't be smoke-tested (e.g. "needs a live Replicate take to confirm the new param", "verify the new ComfyUI node mapping on a real local render")

## Phase 6: Review & Ship

20. **Commit** — Descriptive message in the project's existing style (imperative, single-line subject, body explains *why* not *what*). Reference the card if useful. Push to the feature branch
21. **Peer review** — Run `/review` (spawns a fresh agent against the branch diff vs `main` with no prior context). It catches logic errors, missed edge cases, convention violations, naming issues we've gone blind to. Fix every finding before proceeding — even minor ones — unless the fix is a major undertaking (in which case track it as a follow-up card)
22. **Pull main into the branch** — `git pull origin main` to pick up anything that landed while you were working. Resolve conflicts using the rules below

### Merge Conflict Rules

22.1. **Default to main's version.** If a conflict is in code you didn't intentionally change, accept main's side. Someone else fixed a bug or added a feature — don't silently revert their work
22.2. **Assume incoming changes are important.** Treat every conflict as "main has a critical fix" until you've read the diff and confirmed otherwise. Be very careful about overwriting new code with your version
22.3. **Only keep your side for lines you specifically wrote.** If you changed a function and main also changed it, read both versions carefully. Merge surgically — keep their fixes, layer your change on top
22.4. **If the merge is messy, restart from main.** When conflicts are widespread or hard to reason about, it's safer to take main wholesale and reimplement your changes on top. A clean re-apply is better than a botched merge
22.5. **Re-read the final result.** After resolving, read through every conflicted file in full. Make sure the merged code actually makes sense — don't just trust the conflict markers

23. **Re-run the smoke suite** — make sure the merge didn't break anything: run all six phases again (Phase 5, step 16)
24. **Return to the root checkout** — `cd` back to the project root (where `main` is checked out). Remaining steps run from here
25. **Open a PR and self-merge** — `gh pr create --fill` then `gh pr merge --merge` (real merge commit, not `--squash`, so the branch's commits stay reachable and step 26's `git branch -d` still works), then `git pull origin main` to fast-forward the root checkout. **No approval needed** — `main` is unprotected on this solo repo, so GitHub disabling "Approve" on your own PR is irrelevant; a required review only applies under a branch-protection rule, of which there is none. The PR is a record/URL with no extra ceremony — don't wait on a human to approve. (Direct `git merge <branch> && git push` is the fallback if `gh` is unavailable.)
26. **Clean up the worktree and branch** — kill any app instance or ComfyUI server still running from the worktree FIRST (it holds the worktree directory lock)
    ```
    git worktree remove .trees/wt<k>
    git worktree prune
    git branch -d <branch>
    git push origin --delete <branch>
    ```
27. **Delete the plan + tracker files** — If the card has a `plans/<file>.md` behind it, delete it now (`git rm plans/<file>.md && git rm plans/tracker_<branch>.md && git commit -m "Remove <name> plan; <feature/fix> is implemented" && git push`). The plans directory is for *open* work only; the tracker doc is per-card scratch
28. **Move card to Done** — `trello --board 6a2d752eee5f9d7478ad3250 card move <card_id> Done`
29. **Comment on the card** — `trello --board 6a2d752eee5f9d7478ad3250 comment add <card_id> "<summary>"`. Include: what changed, which files, what it fixes/adds, the commit hash(es), and what needs manual testing. Use real newlines in the text, not `\n` escapes. Leaves a paper trail for future debugging
30. **Create follow-up cards** — If review, implementation, or testing surfaced issues that are out of scope for this card (pre-existing bugs, minor improvements, edge cases deferred as too risky to bundle), create new Trello cards (`trello --board 6a2d752eee5f9d7478ad3250 card add "To Do" "<title>" "<desc>"`). Reference the original card so there's a trail. Don't let follow-up work disappear into commit messages — if it's worth noting, it's worth tracking
31. **Write an overview of the changes made** — As the final step, post a concise overview to the user summarizing the work: what changed (the user-facing behavior delta, not a file list), which files were touched, anything that still needs manual testing or follow-up, and the commit hash(es) and merged branch. This is the closing handoff — it's how the user picks the session up cold and knows the card is actually shipped

## Phase 7: Clean up

Stop any app instances or ComfyUI servers you've started :)

---

## Quick Reference: Card Categories

| Category | Key concerns |
|----------|-------------|
| **Project document / persistence** | `store/project.py` + `store/models.py`. Writes are write-through off worker threads — serialize under the `RLock`, atomic writes with unique temp names (avoids `WinError 32`). Shots buffer (`dirty`); takes persist immediately to `takes.json`. Any schema change needs a load-time migration |
| **Backends — hosted** | `backends/replicate_client.py`. Cost-confirm gate before every launch. `model_library.json` is authored (verified IDs); per-param schemas fetched live via the **Model Library** tab's *Fetch live schemas* button and cached in `data/schema_cache.json` (`store/schema_cache.py`), then read by the shot editor. A live take spends money — explicit go-ahead only |
| **Backends — local (ComfyUI)** | `backends/comfy_client.py` + lifecycle/preflight. MUST run `--disable-dynamic-vram` (GPU watchdog/TDR crash); `preflight()` refuses otherwise. Probing a down port costs a full socket timeout on this machine — keep status polling off the GUI thread. A live take uses GPU — explicit go-ahead only |
| **Framing pipeline** | `pipeline/framing.py` — `normalize_keypose`, `canvas_size` (hosted: longest side 1254; local: ~410k-px budget snapped to /16), `render_keyposes` keys + places sprites at generation time. No baked keypose files. Eyeball across wide/tall aspects |
| **Assets & takes** | Additive: importing an asset COPIES into `.assets/`; bin/restore only touches files under the project. Never move/delete external originals. Each take's `settings_snapshot` is immutable |
| **UI / tabs** | `ui/` — closable tabbed central widget (Shots / Assets / Model Library / ComfyUI Status), shot tab editor, placement canvas, cost-confirm gate. UI reads the project, never owns persistence. Never call a modal's `.exec()` in headless tests |
| **Refactoring** | High blast radius if it touches the persistence layer, the backend threading model, or the framing contract. Run the full six-phase smoke suite, then a manual launch of the app |
