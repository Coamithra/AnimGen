# Plan: Auto-sync model options from Replicate's live schema

Trello card #18 (`6a3180fd`). Follow-up from #15 (PR #8).

## Context

The shot editor builds the resolution / duration / mode dropdowns from
`enum or model.get(<field>)`, where `enum` comes from `schema_cache.get(...)` (populated
by the Model Library tab's *Fetch live schemas*, which calls
`replicate_client.get_input_schema`).

**Bug:** Replicate stores those enums as `$ref` / `allOf` references into
`components.schemas`, NOT inline on the property. `get_input_schema` only extracts the
`Input` component's `properties` and throws away the sibling component schemas that hold
the actual enums. Confirmed live in `data/schema_cache.json`:

```json
"resolution": {"allOf": [{"$ref": "#/components/schemas/resolution"}], "default": "720p", ...}
```

`schema_prop.get("enum")` is therefore always `None`, so the editor silently falls back to
the hand-authored `resolution_options` / `duration_range` / `mode_options` in
`model_library.json` — which can drift from Replicate again (the exact drift #15 fixed by
hand).

## Design

### Part A — Resolve enum `$ref`s at fetch time (the real fix)

`backends/replicate_client.py`:

- Keep `get_input_schema` returning `(props, required)`, but before returning, **inline**
  each property's referenced enum. Add pure helpers (testable without network):
  - `_deref(ref, schemas)` — resolve `"#/components/schemas/<name>"` against the full
    `components.schemas` dict.
  - `_follow_enum(prop, schemas)` — look at a property's direct `$ref` and its
    `allOf` / `anyOf` / `oneOf` lists; return the first referenced (or inline) `enum` +
    its `type`.
  - `_resolve_enums(props, schemas)` — for every property missing an inline `enum`,
    attach `enum` (and `type` if absent) from `_follow_enum`. Returns a new dict (does not
    mutate the input).
- `get_input_schema` now reads the full `components.schemas`, extracts `Input.properties`,
  and returns `_resolve_enums(props, schemas)`.

Effect: `schema_cache.json` props carry inline `enum` lists. `ui/shot_tab.py`
(`_make_output_widget`, `_make_param_widget`) already does
`enum = schema_prop.get("enum")` then `opts = enum or model.get(...)` — so **live options
now win automatically and the authored lists become a genuine fallback**. No editor change
required.

### Part B — "Update model data on startup" setting

- New `store/app_settings.py` + `data/app_settings.json` (own file, NOT `app_state.json`
  which `_remember_last` rewrites wholesale). Mirrors `schema_cache` / `prompt_library`
  discipline: lock-guarded, atomic write via `store.project._atomic_write_json`, tolerant
  read. Minimal typed API: `get_bool(key, default)` / `set_bool(key, value)`. Initial
  key: `update_schemas_on_startup` (default `False` — opt-in, since it spends a few
  schema-read API calls and needs a token).
- `ui/main_window.py`: add a **Settings** menu with a checkable action
  "Update Replicate model data on startup", reflecting/writing the setting.
- **Startup trigger:** after the Model Library tab is built, if the setting is on, kick
  off the existing off-thread `_SchemaFetcher` (no GUI block, no token → it already
  reports per-model failure and we just log it). Reuses the exact path the manual button
  uses, so there's one code path.
- **Model Library tab:** the existing *Fetch live schemas* button stays the models-tab
  manual "update now" control — per the user's call, the on-startup toggle lives only in
  the Settings menu (no new tab widget).

## Tests

- Extend a smoke phase (whichever already covers the Replicate client / schema cache —
  likely `smoke_phase` for backends/Model Library) with a pure-function test of
  `_resolve_enums` against a synthetic openapi schema (Input prop with
  `allOf: [{$ref}]` + a sibling enum component → asserts the inlined `enum`). Covers
  `$ref`, `allOf`, `anyOf`/`oneOf`, and the no-enum passthrough.
- Pure test of `app_settings` round-trip (set → get → reload) against an overridden
  `paths.APP_SETTINGS` tempdir.
- Smoke for the Settings menu action + models-tab checkbox staying in sync (headless,
  no `.exec()`).

## Out of scope

- Editing `model_library.json` automatically (it stays authored; the authored lists
  remain the offline fallback).
- A live Replicate take. Verify offline (the schema fetch is a read, no spend; dry_run
  for build).
- Caching policy / TTL / background refresh beyond the startup trigger.

## Verification

- Run all six headless smoke phases.
- Manual: open Model Library tab → *Fetch live schemas* → open a shot on a Replicate
  model → confirm resolution/duration dropdowns now show the live enum values; toggle the
  startup setting and confirm it persists across a relaunch and triggers a fetch.
