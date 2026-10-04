# RULES — Autonomous Android

_Last updated: 2026-10-04._

These rules apply to AI-generated and human-written code equally. They encode
what this project has already learned the hard way; the Lessons section of
`MEMORY.md` gives the incidents behind them.

---

## General

- Read the relevant doc before changing behaviour. If code and docs disagree,
  resolve it explicitly — do not silently pick one.
- Make the smallest reasonable change. Do not refactor unrelated code.
- Preserve working behaviour unless a requirement changes it.
- Never invent a requirement. Put it in PRD.md § Open Questions and ask.
- `bot.py` and `config.py` are the frozen legacy v1. Do not "fix" them; they
  are lint-excluded on purpose (`config.py`'s placeholder is deliberate).

## The safety rules (non-negotiable)

1. **No shell.** No tool takes free-form shell input. `shell=True` is banned.
   Always argv arrays.
2. **The model is untrusted.** Model text never authorises anything. Policy
   decisions derive from declared risk and actor only.
3. **Validate before execute.** Every handler runs only on schema-validated
   arguments.
4. **Bind approvals to a hash** of tool + version + exact arguments. An
   approval must never transfer to different arguments or another tool.
5. **Never claim unverified success.** If the tool cannot confirm the action
   happened, say so and return an error code.
6. **No third-party network calls** from tools.
7. **Secrets only in `.env`.** Never in code, docs, logs, audit or commits.
8. **Audit everything executed**, with an error code on failure — but never
   the human summary, which can quote device content.

## Testing

- Every behaviour change needs a test. Relevant tests must pass before a task
  is complete.
- **Mutation-check every regression test.** Deliberately reintroduce the bug
  and confirm the test fails. A test that passes with the fix removed is
  worse than no test, because it creates false confidence. This has caught
  several worthless tests in this project.
- Prefer tests that exercise real seams over mocks agreeing with each other.
  The end-to-end scheduling tests exist because every seam here has produced
  a bug.
- A flaky test is a finding, not a nuisance. The scheduler double-run race
  surfaced as a ~40% intermittent failure.
- Name tests as behaviour: `test_approval_does_not_transfer_to_different_arguments`.

## Code quality

- Descriptive names; focused functions; no dead code.
- Comments explain **why**, especially where the code looks odd because
  Android forced it. Those comments are load-bearing — do not strip them.
- Prefer stdlib. Justify any new dependency against: does the project already
  do this? does the stdlib? what is the maintenance cost on Termux?
- Type hints on public functions; validate all external input.
- Run `python -m ruff check .` before committing (line length 120, py310).
  Note: ruff is **not** preinstalled in a fresh sandbox — `pip install ruff`.

## Tools (adding or changing one)

- One narrow tool per action. No generic `control_device(action, value)`.
- Put bounds in the schema: enums, patterns, min/max, string lengths.
- Write the description for the model: when to use it, when **not** to, and
  what to tell the owner if it fails.
- Choose the `Risk` level honestly; it is the only thing standing between the
  model and the action.
- Return a deterministic summary. Include units and accuracy where relevant.
- Handle the three Termux:API failure modes: empty output with exit 0, an
  `API_ERROR` object, and a timeout.

## Scheduling

- A scheduled run has nobody present. Anything approval-gated must be refused
  unless the owner pre-authorised that exact action.
- Never leave a pending approval from an unattended run.
- Blocked must not mean disabled: a blocked recurring task still reschedules.
- Skip missed slots; never replay them.

## Internationalisation (from now on)

- **No new hard-coded user-facing English.** Route new strings through
  whatever mechanism Phase 4 lands; until then, add the string to the Phase 4
  inventory in TASKS.md in the same change.
- Never concatenate sentence fragments to build a message; it does not
  translate. Use whole templates with named placeholders.
- Do not assume ASCII, LTR, Latin digits, or English date formats.
- Keyword matching against user text (as `SkillRouter` does today) is an
  English-only technique; do not add more of it.

## Git

- Conventional prefixes: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
  `chore:`. _(Existing history predates this and uses plain imperative
  subjects; do not rewrite it.)_
- The body should say **why**, and name what was mutation-checked.
- Never commit secrets. `.git/config` does not survive this workspace's
  snapshots — re-add `origin` before pushing.
- `git commit` here needs explicit `-c user.email=... -c user.name=...`.

## Documentation

- Update `TASKS.md` status in the same change as the code.
- Add to `MEMORY.md` when a decision or lesson should outlive the session.
- When editing docs programmatically, **verify the edit landed**. `str.replace`
  fails silently on a miss, and multi-word checks break on line wrapping — use
  a whitespace-tolerant regex (`re.search(r'phrase\s+here', text)`). This has
  caused four silent no-op doc edits in this project.

## Definition of Done

- [ ] Requirement and acceptance criteria satisfied
- [ ] Safety rules upheld (no shell, validated, policy-gated, hashed, audited)
- [ ] Tests added **and mutation-checked**
- [ ] `ruff check .` clean, full suite green
- [ ] Error, empty and loading states handled where relevant
- [ ] Accessibility considered (both themes, keyboard, contrast)
- [ ] No new hard-coded English, or it is logged in the Phase 4 inventory
- [ ] No unnecessary dependency added
- [ ] `TASKS.md` updated; `MEMORY.md` updated if durable
