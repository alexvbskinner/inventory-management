---
name: vue-optimize
description: Analyze Vue 3 component structure and suggest performance + code-reuse optimizations. Use when reviewing .vue files for duplicated helpers, expensive computeds, unsafe v-for keys, extractable composables, or render performance in client/src.
---

# Vue Optimization Analysis

Structured playbook for analyzing Vue 3 components in this project and proposing
high-leverage performance and code-reuse improvements. It pairs a heuristic
scanner (`scan_vue.py`) with a judgement checklist so findings are grounded in
metrics, not vibes.

## This project's conventions (read first)

Recommendations MUST fit these patterns — don't suggest rewrites that fight them.

- **Component style is mixed and intentional**: views and `App.vue` use the
  **Options API with `setup()`**; reusable modals/small components use
  **`<script setup>`**. Match whichever style the file already uses. Do not
  convert a whole view to `<script setup>` as an "optimization."
- **State**: raw async data lives in `ref()` (`allOrders`, `inventoryItems`,
  `loading`, `error`); derived data lives in `computed()`. No `reactive()`
  objects. Keep it that way.
- **Shared logic already lives in composables** under `client/src/composables/`:
  - `useFilters.js` — singleton filter refs + `getCurrentFilters()` / `resetFilters()`
  - `useI18n.js` — `t()`, `currentLocale`, `currentCurrency`, `translateProductName/CustomerName/Warehouse`
  - `useAuth.js` — mock current user
  These are **module-level singletons** (refs declared at module scope), so new
  shared helpers should follow the same singleton pattern.
- **i18n is hand-rolled** (object-path traversal in `useI18n`, locales in
  `client/src/locales/{en,ja}.js`) — NOT `vue-i18n`. Any new user-facing string
  needs an `en` + `ja` key. Don't propose adding a i18n library.
- **No build-time perf tooling** (no `@vueuse/core`, no virtual scroller). Only
  recommend a new dependency when the payoff is large and call it out explicitly.

## Step 1 — Run the scanner

```bash
python3 .claude/skills/vue-optimize/scan_vue.py client/src
# JSON for programmatic use / diffing:
python3 .claude/skills/vue-optimize/scan_vue.py client/src --json
```

Stdlib only — no install. It reports two layers:

1. **Cross-file reuse candidates** — helpers defined in ≥2 files (the highest-leverage finding).
2. **Per-file flags** — API style, ref/computed counts, unsafe `v-for` keys,
   loop-heavy computeds, unvirtualized table row-loops, inline template work,
   and the loading/error/try-finally boilerplate.

The scanner is **heuristic** (regex, not a real parser). Every flag is a lead to
confirm by reading the file, not a verdict. Open each flagged `file:line` before
recommending a change.

## Step 2 — Triage with the checklist

### A. Code reuse (usually the biggest win here)

For each cross-file reuse candidate, confirm the definitions are actually
equivalent, then recommend extraction to the **existing** composable that owns
that concern:

| Duplicated helper | Where it belongs |
|---|---|
| `currencySymbol` | add to `useI18n.js` (it already owns `currentCurrency`) and import it |
| `formatDate` / `formatDateShort` | `useI18n.js` (locale-aware) or `client/src/utils/date.js` |
| `translateCategory` / `translatePriority` / `translateStockLevel` | `useI18n.js` or a `useTranslations` composable, backed by the locale files |
| `getStockStatus` / `getStockStatusKey` / `getStockBadge` | a `useInventoryStatus` composable (pure logic, no I/O) |
| loading/error/`try/finally` data fetch | a `useDataLoader(apiCall, deps)` composable returning `{ data, loading, error, reload }` |
| `showXModal` + `selectedX` + open/close | a `useModal(initialState)` composable |

When proposing an extraction:
- Keep the **exact behavior** (same locale handling, same fallbacks).
- Update **all** call sites; don't leave half the views on the old copy.
- Preserve the singleton pattern for shared state; keep pure helpers stateless.
- Anything user-facing still needs `en` + `ja` keys.

### B. Render performance

- **Unsafe `v-for` keys** (`:key="index"` / `:key="idx"` / missing): real
  correctness bug, not just perf — breaks Vue's list diffing on insert/reorder.
  Replace with a stable domain id (`sku`, `id`, `month`, `order_number`). This
  is item 1 in CLAUDE.md's Common Issues. **Fix these first.**
- **Loop-heavy computeds**: fine when inputs are small and stable (a `computed`
  caches until deps change). Scrutinize only when the source array is large or
  the computed chains multiple passes (`.filter().map().sort()` over the same
  data) — collapse passes, or push aggregation to the API. Don't move correct,
  cached `computed`s into methods (that's slower — methods re-run every render).
- **Unvirtualized tables**: every row-loop renders all rows. Acceptable for the
  bounded demo data (months, categories, top-N). Flag virtualization **only** if
  a table can realistically render hundreds+ of rows; otherwise note it and move on.
- **Inline template work** (`.toLocaleString()`, arithmetic in bindings): minor.
  Lift to a `computed` only when it's in a hot loop or repeated many times.
- **Filter/search inputs**: if a text input drives a server refetch via `watch`,
  recommend debouncing the watcher. There's no `@vueuse/core` here — a tiny
  local debounce util is the right call, not a new dependency.

### C. Structure / maintainability

- Very large views (e.g. `Dashboard.vue`, `Spending.vue` are 800–1300 loc with
  10+ computeds) are extraction candidates: pull cohesive chart/table sections
  into child components, and pull their derived-data computeds into composables.
- A view with many `ref`s feeding one big `computed` often wants the derivation
  in a composable so it's testable in isolation.

## Step 3 — Report findings

Produce a prioritized list, highest leverage first. For each finding give:

1. **What** + `file:line` (from the scanner, confirmed by reading).
2. **Why it matters** (correctness? wasted renders? duplication drift risk?).
3. **Concrete fix** that fits this project's conventions (name the target
   composable, note `en`/`ja` keys if user-facing).
4. **Effort / blast radius** (how many call sites change).

Default ordering: unsafe `v-for` keys → cross-file duplication → genuinely
expensive computeds → structural splits → minor inline cleanups.

Don't apply changes unless asked. If asked to implement, **delegate `.vue` edits
to the `vue-expert` subagent** per CLAUDE.md ("ANY time you need to create or
significantly modify a .vue file, you MUST delegate to vue-expert"), then have
`code-reviewer` check the result.

## Guardrails

- The scanner is advisory and regex-based — never cite a flag you haven't opened
  the file to confirm. Comments and strings can trigger false positives.
- Don't recommend Options-API↔`<script setup>` conversions as optimizations.
- Don't add dependencies or an i18n library without explicitly justifying it.
- Preserve behavior exactly when extracting shared code; migrate every call site.
- Keep new user-facing strings bilingual (`en.js` + `ja.js`).
