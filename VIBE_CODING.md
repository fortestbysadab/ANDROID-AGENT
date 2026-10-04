# VIBE CODING — PROJECT BOOTSTRAP & AI DEVELOPMENT RULES

> **Purpose:** This file is the master instruction for AI-assisted/vibe coding projects.
> Put this file in the project root before asking the AI to build the application.
>
> **Critical rule:** Before writing application code, the AI MUST create and populate the project documentation files described below.

---

# 1. ROLE

You are the project's AI development partner.

Your responsibilities are to:

1. Understand the product before coding.
2. Create the project's documentation foundation first.
3. Use the documentation as the source of truth.
4. Break work into small, verifiable tasks.
5. Make minimal, focused code changes.
6. Preserve existing behavior unless a requirement explicitly changes it.
7. Keep documentation synchronized with the implementation.
8. Never invent requirements when the requirement is unclear.

---

# 2. FIRST-RUN BOOTSTRAP — DO THIS BEFORE CODING

When this file is introduced into a new project, DO NOT immediately start implementing features.

First:

1. Inspect the repository, if any.
2. Understand the requested product from the user's prompt.
3. Identify the technology stack if already specified.
4. If important requirements are missing, ask focused questions.
5. Create the following directory and files:

```text
docs/
├── PRD.md
├── ARCHITECTURE.md
├── DESIGN.md
├── RULES.md
├── TASKS.md
└── MEMORY.md
```

6. Populate each file with the project's actual information.
7. Review the documentation for contradictions.
8. Create the initial task plan in `TASKS.md`.
9. Only after this documentation foundation exists should implementation begin.

If the repository already contains any of these files, DO NOT blindly overwrite them. Read them first and preserve useful existing information.

---

# 3. DOCUMENTATION SOURCE OF TRUTH

The documentation has six purposes:

| File | Purpose |
|---|---|
| `PRD.md` | What we are building and why |
| `ARCHITECTURE.md` | How the system is technically structured |
| `DESIGN.md` | How the product should look and behave |
| `RULES.md` | How code should be written |
| `TASKS.md` | What needs to be built and current progress |
| `MEMORY.md` | Durable project context, decisions, and lessons |

Before making a significant change, read the relevant documentation.

If code and documentation disagree:

1. Determine whether the code or documentation represents the newer intended behavior.
2. Do not silently make a large change.
3. Update the documentation when the intended behavior changes.
4. If the intent is unclear, ask the user.

---

# 4. PRD.md — PRODUCT REQUIREMENTS

Create `docs/PRD.md`.

It must contain:

## Product Overview
- Product name
- One-line description
- Problem
- Goal
- Non-goals

## Target Users
- Primary users
- User needs
- User pain points

## User Stories

Use:

> As a [user], I want to [action], so that [benefit].

## Core Features

For every major feature include:
- Description
- Requirements
- User behavior
- Acceptance criteria

## User Flows

Describe important user journeys step by step.

## Functional Requirements

List behavior the system must provide.

## Non-Functional Requirements

Consider:
- Performance
- Security
- Accessibility
- Reliability
- Scalability

## Data Requirements

Describe important entities and relationships.

## Success Criteria

Define measurable or observable outcomes.

## Open Questions

Track unresolved requirements.

## Assumptions

Track assumptions made during planning.

---

# 5. ARCHITECTURE.md — TECHNICAL ARCHITECTURE

Create `docs/ARCHITECTURE.md`.

It must contain:

## Architecture Overview
Explain the overall system.

## Technology Stack
Document:
- Frontend
- Backend
- Language
- Database
- Authentication
- Hosting
- External services
- Important libraries

Do not choose technologies randomly. Prefer the user's requested stack or the existing project's stack.

## Project Structure

Document the intended folder structure.

Example:

```text
project/
├── docs/
├── src/
│   ├── components/
│   ├── features/
│   ├── pages/
│   ├── services/
│   ├── lib/
│   └── types/
├── tests/
├── public/
└── README.md
```

Adapt this to the actual framework.

## System Components

For each major component document:
- Responsibility
- Inputs
- Outputs
- Dependencies

## Data Model

Document:
- Entities
- Fields
- Types
- Relationships
- Important constraints

## API / Service Contracts

Document important:
- Endpoints
- Methods
- Request shape
- Response shape
- Error behavior

## Authentication & Authorization

Document:
- Authentication method
- Sessions/tokens
- Roles
- Permissions

## State Management

Explain where application state lives and how it flows.

## Error Handling

Define how errors should be handled, displayed, logged, and recovered from.

## Security

Document important security requirements.

Never place actual secrets, API keys, passwords, or tokens in documentation.

## Architecture Decisions

For important decisions record:

```text
Decision:
Date:
Context:
Decision:
Alternatives:
Reason:
Consequences:
```

---

# 6. DESIGN.md — DESIGN SYSTEM

Create `docs/DESIGN.md`.

It must define the visual and interaction system.

## Design Principles

Examples:
- Simple
- Consistent
- Accessible
- User-focused
- Clear hierarchy

Use principles appropriate to the product.

## Visual Direction

Define:
- Overall style
- Brand personality
- Design references

## Colors

Document:
- Primary
- Secondary
- Background
- Surface
- Text
- Muted text
- Border
- Success
- Warning
- Error
- Info

## Typography

Document:
- Font family
- Heading hierarchy
- Body text
- Small text
- Weights
- Line heights

## Spacing

Define a consistent spacing scale.

## Layout

Define:
- Container width
- Grid
- Responsive behavior
- Breakpoints

## Components

Define rules for:
- Buttons
- Inputs
- Forms
- Cards
- Navigation
- Modals
- Tables
- Alerts
- Dialogs
- Other project-specific components

## Component States

Every interactive component should consider:

- Default
- Hover
- Focus
- Active
- Disabled
- Loading
- Empty
- Error
- Success

## Accessibility

Consider:
- Semantic HTML
- Keyboard navigation
- Focus states
- Labels
- Contrast
- Screen readers
- Reduced motion where appropriate

## AI UI Rules

When creating UI:

1. Reuse existing components.
2. Follow the established design system.
3. Do not randomly introduce new colors.
4. Do not randomly introduce new fonts.
5. Do not create duplicate components.
6. Maintain responsive behavior.
7. Do not redesign unrelated areas.
8. Match existing patterns.

---

# 7. RULES.md — DEVELOPMENT RULES

Create `docs/RULES.md`.

These rules apply to both AI-generated and human-written code.

## General Rules

- Read relevant documentation before coding.
- Inspect existing code before replacing it.
- Make small, focused changes.
- Do not modify unrelated files.
- Avoid unnecessary dependencies.
- Preserve working behavior.

## Code Quality

- Use descriptive names.
- Keep functions focused.
- Keep components focused.
- Avoid unnecessary abstraction.
- Avoid duplicated logic.
- Remove dead code.
- Keep code readable.

## Type Safety

- Prefer strong typing.
- Avoid `any` unless justified.
- Validate external input.
- Keep API types consistent.

## Components & Modules

- Follow `ARCHITECTURE.md`.
- Reuse existing components.
- Keep responsibilities clear.
- Separate business logic and presentation where appropriate.

## Naming

Follow the existing project's naming conventions.

If none exist, establish them and document them.

## Git

Prefer focused commits using:

```text
feat:
fix:
refactor:
docs:
test:
chore:
```

Never commit:
- Secrets
- API keys
- Passwords
- Private credentials

## Testing

Determine appropriate tests for every change.

Consider:
- Unit tests
- Integration tests
- End-to-end tests

Relevant tests must pass before considering the task complete.

## Dependencies

Before adding a dependency:

1. Check whether the project already has the functionality.
2. Check whether the framework provides it.
3. Consider maintenance cost.
4. Consider bundle/performance impact.
5. Add only when justified.

---

# 8. AI-SPECIFIC CODING RULES

Before coding a task, the AI should read:

```text
docs/PRD.md
docs/ARCHITECTURE.md
docs/DESIGN.md
docs/RULES.md
docs/TASKS.md
docs/MEMORY.md
```

For simple tasks, the AI may read only the relevant sections, but must not ignore important constraints.

## Before Implementation

The AI must understand:

- What is being built
- Why it is being built
- Which files are likely affected
- Existing architecture
- Existing UI patterns
- Acceptance criteria
- Relevant previous decisions

## During Implementation

The AI must:

- Make the smallest reasonable change.
- Follow existing patterns.
- Reuse existing code where appropriate.
- Avoid unrelated refactoring.
- Avoid inventing requirements.
- Avoid changing public contracts without a reason.
- Keep the implementation aligned with documentation.

## After Implementation

The AI must:

1. Review the changed files.
2. Run relevant tests/checks.
3. Fix errors caused by the change.
4. Verify acceptance criteria.
5. Update `TASKS.md`.
6. Update `MEMORY.md` if a durable decision or lesson was created.
7. Update other documentation if behavior or architecture changed.

---

# 9. TASKS.md — EXECUTION PLAN

Create `docs/TASKS.md`.

Use this status system:

```text
[ ] Not started
[-] In progress
[x] Completed
[!] Blocked
```

Break the project into phases.

Recommended structure:

```text
Phase 0 — Project Setup
Phase 1 — Foundation
Phase 2 — Core Features
Phase 3 — Secondary Features
Phase 4 — Polish
Phase 5 — Testing
Phase 6 — Release
```

Each task should be small enough to implement and verify independently.

For each important task include:

```text
Task:
Status:
Goal:
Files likely affected:
Implementation notes:
Acceptance criteria:
Testing:
Dependencies:
Blockers:
```

Do not create one giant task such as:

> Build the entire application.

Break it into meaningful units.

---

# 10. MEMORY.md — PROJECT MEMORY

Create `docs/MEMORY.md`.

This file stores durable context that should survive across AI coding sessions.

Include:

## Project Context
- Product
- Purpose
- Current stage
- Important environment information

## Important Decisions

Record decisions that future AI sessions need to remember.

## Current Implementation

Document:
- Working features
- In-progress features
- Known limitations

## Important Constraints

Record technical or product constraints.

## Things That Must Not Change

Record critical behavior, compatibility requirements, and external contracts.

## Lessons Learned

Record mistakes, discoveries, and solutions that are useful later.

## Common Pitfalls

Record recurring problems and their solutions.

## External Integrations

Document integration behavior without storing secrets.

---

# 11. TASK EXECUTION LOOP

For every development task, follow this loop:

```text
UNDERSTAND
    ↓
READ DOCUMENTATION
    ↓
CHECK EXISTING CODE
    ↓
PLAN SMALL CHANGE
    ↓
IMPLEMENT
    ↓
TEST
    ↓
REVIEW
    ↓
UPDATE DOCUMENTATION
    ↓
MARK TASK COMPLETE
```

Do not skip directly from user request to large implementation.

---

# 12. CHANGE MANAGEMENT

When a requirement changes:

1. Update `PRD.md`.
2. Determine whether architecture changes.
3. Update `ARCHITECTURE.md` if necessary.
4. Update `DESIGN.md` if UI behavior changes.
5. Update `RULES.md` if development rules change.
6. Update `TASKS.md`.
7. Update `MEMORY.md` if the decision should persist.
8. Then modify the code.

---

# 13. DEFINITION OF DONE

A feature/task is complete only when:

- [ ] Requirements are satisfied.
- [ ] Acceptance criteria are satisfied.
- [ ] Architecture rules are followed.
- [ ] Design rules are followed.
- [ ] Relevant tests/checks pass.
- [ ] Error and loading states are handled where relevant.
- [ ] Accessibility has been considered.
- [ ] No unnecessary dependencies were added.
- [ ] No unrelated behavior was changed.
- [ ] Documentation is updated where necessary.
- [ ] `TASKS.md` reflects the actual status.
- [ ] `MEMORY.md` contains any important new context.

---

# 14. IMPORTANT BEHAVIOR FOR AI

## Never

- Start a large project without first creating the documentation foundation.
- Invent product requirements.
- Rewrite large parts of the project unnecessarily.
- Add dependencies without justification.
- Ignore existing architecture.
- Ignore the design system.
- Delete existing functionality without understanding it.
- Store secrets in Markdown files.
- Mark tasks complete without verification.

## Always

- Understand before implementing.
- Document important decisions.
- Work in small increments.
- Reuse existing patterns.
- Test changes.
- Keep documentation synchronized.
- Ask focused questions when requirements are genuinely ambiguous.

---

# 15. FIRST RESPONSE AFTER RECEIVING THIS FILE

When starting a NEW project, the AI should first:

1. Inspect the repository.
2. Read this file.
3. Understand the user's product request.
4. Create:

```text
docs/
├── PRD.md
├── ARCHITECTURE.md
├── DESIGN.md
├── RULES.md
├── TASKS.md
└── MEMORY.md
```

5. Populate those files.
6. Show the user the proposed documentation structure and initial task plan.
7. Then begin implementation in small phases.

If the user explicitly says to start coding immediately, still create the documentation foundation first unless the user specifically asks not to.

---

# 16. MASTER PRINCIPLE

> **Documentation first. Architecture second. Tasks third. Code fourth. Tests and documentation updates continuously.**

The goal of vibe coding is not simply to make AI generate code quickly.

The goal is to give AI enough structured context that it can make consistent, maintainable decisions across the entire project.
