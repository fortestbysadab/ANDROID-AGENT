# DESIGN — Autonomous Android

_Last updated: 2026-10-04, written against commit `37b3e57`._

This product has two surfaces: a **conversational surface** (Telegram and the
web console transcript) and a **web console UI**. Both already exist. This
file documents the system as built, so future work matches it rather than
inventing a second style.

---

## Design Principles

1. **Honest over reassuring.** Never report success for something unverified.
   A tool that cannot confirm an action says so. This is the project's
   defining rule and it outranks tidiness.
2. **Say the remedy, not just the fault.** "Grant Termux 'Display over other
   apps'" beats "the call failed". Every known failure class names its fix.
3. **Deterministic text for facts.** Tool summaries are written by application
   code, not the model, so numbers and statuses cannot drift.
4. **Quiet when idle, loud when wrong.** Routine success is brief; failures
   are explicit.
5. **Accessible by default.** WCAG 2.2 AA is the floor, verified independently
   in both themes.
6. **No gratuitous chrome.** The web console is one self-contained HTML file;
   it must render on an offline phone.

## Visual Direction

Clean product UI in the register of ChatGPT/Claude: neutral surfaces, a single
accent, generous whitespace, text-first. No illustrations, no gradients as
decoration, no brand mascot. The device is the product; the UI is a window.

## Colors

Defined as CSS custom properties in `android_agent/web/ui.html`, with a light
palette as the base and a dark palette authored separately (not a mechanical
inversion). `:root { color-scheme: light dark }`, with
`@media (prefers-color-scheme: dark)` plus an explicit
`html[data-theme="light"|"dark"]` override persisted in `localStorage` and
applied by an inline script **before first paint** to avoid a flash.

| Token | Role |
|---|---|
| `--bg` | page background |
| `--surface` | cards, composer, sidebar |
| `--surface-2` | raised/hover surface |
| `--text` | body text (≥ 4.5:1 on `--bg` in both themes) |
| `--muted` | secondary text (≥ 4.5:1) |
| `--border` | 1 px separators (≥ 3:1 where load-bearing) |
| `--accent` | primary action, focus ring |
| `--user-bubble` | owner message background |
| `--assistant-bubble` | agent message background |
| `--ok` / `--warn` / `--danger` | status, never colour-alone |

Rules: do not introduce a colour outside these tokens; do not express state by
colour alone — pair with an icon, label or shape.

## Typography

System font stack only (no webfont — the page must work offline):
`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans", sans-serif`.

> **Multilingual note:** `Noto Sans` is included deliberately so Devanagari,
> Bengali and other scripts render rather than showing tofu. Any future font
> work must keep a Noto fallback.

| Role | Size | Weight | Line height |
|---|---|---|---|
| Message body | 15–16 px | 400 | 1.55 |
| Heading (sidebar/brand) | 15 px | 600 | 1.3 |
| Small / timestamps | 12–13 px | 400 | 1.4 |
| Code / `pre` | 13 px mono | 400 | 1.5 |

Body text is never below 15 px; chat UIs that go smaller fail readability at
default system sizes.

## Spacing

A 4 px base scale: `4, 8, 12, 16, 24, 32, 48`. Bubble padding `10px 14px`.
Gap between grouped messages `4px`; between authors `16px`.

## Layout

- Max transcript column ~`68ch`; bubbles max ~75% of the column.
- Two-pane grid: sidebar (`260px`) + main. Under **860 px** the sidebar
  becomes an off-canvas drawer with a scrim; Escape closes it.
- Composer is sticky at the bottom; the transcript scroll-pins to the bottom
  only when the user is already within 90 px of it.
- Logical properties (`padding-inline-start`, `inset-inline`) throughout, so
  an RTL language does not need a second stylesheet.

## Components

**Message bubble.** Radius 8–16 px (never a full pill — it breaks on multi-line).
Author distinguished by **both** alignment and colour. Agent messages carry a
visible label/icon and are never styled to look like the owner's.

**Composer.** Auto-growing textarea, Enter sends, Shift+Enter newlines,
explicit send button ≥ 44 px.

**Approval prompt.** Shows the exact tool and arguments being approved —
never a paraphrase. Two clearly distinct actions; the destructive one is not
the default focus.

**Scheduled-task list.** One line per task: id, description, schedule, next
run **with timezone**, and for finished one-offs "done 2h ago" with no next
run. Showing the timezone is a design requirement, not decoration: it is how a
timezone fault becomes visible instead of silent.

**Location card.** Two numbers are not an answer to "where am I". A location
result renders coordinates, accuracy, an *Open in Maps* link (which hands off
to the phone's real map app — better than an embedded frame on mobile) and a
*Show map* button.

The map is **loaded only on tap**. This is the single external resource in
the console, and making it opt-in preserves the rule that the page works on
an offline phone: before that tap, nothing has been requested from anyone but
this server. A coarse fix also carries its warning here, not only in the
prose.

**Media drawer.** Grid of captured artifacts; tapping opens the file inline.

**Toast.** Transient, polite, never the only channel for an error.

## Component States

Every interactive component must define: default, hover, focus-visible,
active, disabled, loading, empty, error, success.

- **Focus** is always visible — a 2 px `--accent` ring, never `outline: none`.
- **Empty transcript** offers suggestion chips, not a blank screen.
- **Loading** during a run shows a typing indicator; under
  `prefers-reduced-motion` this is a static "working…" label.
- **Failed send** offers inline Retry; a message is never silently dropped.

## Conversational Surface (Telegram + transcript)

This is the primary UI and has its own rules:

- **Status glyphs** for scheduled results: ⏰ ran, 🔒 blocked, ⚠️ failed.
- **Deterministic summaries.** `Location: 22.36464, 87.99950 - accurate to
  about 8 m - via gps.` Numbers come from code, not prose.
- **Uncertainty is stated inline**, not implied: a fix wider than 300 m says
  it is a coarse estimate and may be far out.
- **Staleness is stated**: "last known fix, 15 min old".
- **Remedies are actionable from another room** — "open Termux" is useless to
  someone messaging from elsewhere; name the setting instead.
- **Rich text** in the web console: a deliberately small Markdown subset
  (bold, italics, inline code, fenced blocks, lists, autolinks), escaped
  before any tag is added.

## Accessibility

- Transcript is `role="log"` `aria-live="polite"`; new messages announce
  author → time → content.
- Targets ≥ 24 px (WCAG 2.2 floor), designed to 44–48 px.
- Full keyboard path: Tab order, Escape closes drawer/overlay, focus moves
  into an overlay on open and returns to the trigger on close.
- Contrast verified **separately** in light and dark.
- `prefers-reduced-motion` respected.
- Every control has an accessible name; icon-only buttons carry `aria-label`.

**Multilingual accessibility (not yet done):** the `<html lang>` attribute is
hard-coded `en`, and `dir` is never set. Both must become dynamic for correct
screen-reader pronunciation and RTL layout. Tracked in TASKS.md Phase 4.

## AI UI Rules

1. Reuse the tokens in `ui.html`; never hard-code a hex value.
2. Do not add a webfont or a CDN reference — the page must work offline.
3. Do not introduce a second component for an existing pattern.
4. Keep both themes correct; check contrast in each.
5. Preserve logical properties; do not reintroduce `left`/`right`.
6. Do not redesign areas unrelated to the task.
7. Any new user-facing string is a **translatable string** — do not add new
   hard-coded English without noting it in TASKS.md Phase 4.
