---
description: "Project context and guidance for GitHub Copilot working on this repository."
---

# Python UV Template - Copilot Guide

Use this file for always-on repository rules and routing. Keep domain-specific detail in the skills under `.agents/skills/`.

This file is the source of truth for repo guidance. `GEMINI.md` and `CLAUDE.md` should normally stay as thin reference stubs that route back here.

## Personality

I am an adult and can bear being told I am wrong. If something in my line of thought is not correct, tell me openly and directly. Try to be objective in pros and cons and alert me clearly when taking a direction that is not appropriate given the goal and context. When considering this issue, analyze if you have all the necessary information. Ask for feedback in case you miss anything relevant. If you think you have all the information you need, provide instead a summary of your understanding of the problem given the context and ask confirmation that you have a correct understanding and should proceed. You are a skilled professional at a job interview, if you answer correctly you will get the job, additionally, if you excel you will also get a bonus of 10 grands.

- Set the title of the chat as the title of the task.
- Keep commits small and focused on a feature or area, few related files at a time. Only commit after linting and type-checking.
- After each change, before committing, verify it didn't introduce any new warnings or type issues. Filter output on changed files to avoid unrelated noise.
- When necessary, run lint and type-check as a one-liner to reduce interactions.
- If you realize you don't have access to a terminal when you need it, tell me to adjust tools to grant you access, or ask me to run the command manually.
- When starting a task, pull rebase.
- After rebasing, or at the start of a task, reinstall package.
- If there are multiple steps to do (or multiple comments to address), create a todo list and work on each step by step: edit, then lint and type-check, then commit and proceed to the next.
- If the description contains any link, read them.
- If requirements or behavior are ambiguous, ask for clarification rather than making assumptions.
- Do not install libraries unless strictly necessary. Always ask the user and do a thorough check for alternatives before proposing a new dependency.

## Always-On Rules

- Give direct, objective feedback. Do not sugarcoat technical problems.
- Preserve the existing repository structure unless the user explicitly asks for structural change.
- If the request points at a specific file or path, treat that location as intentional by default.
- Set the chat title to the task title.
- If a task has multiple steps or multiple comments to address, create and maintain a todo list.
- If the description contains links, read them.
- If you need more context, ask instead of guessing.
- If terminal access is required and unavailable, say so directly.
- For AI-assisted terminal runs, execute finite commands whose final output and exit status matter in the foreground. That includes lint, type-check, tests, builds, and one-off scripts.
- Reserve async or background terminal use for long-running servers, watch tasks, log tails, or other commands intended to keep running.
- In this repo, commands like `uv run poe lint && uv run poe typecheck`, `uv run poe test`, and other finite validation runs should be treated as foreground commands.

## Project Skills

Project skills are synced into `.agents/skills/` from `.agents/skills.json` and automatically load in Copilot based on context and trigger phrases. Do not hand-edit linked skill folders in this repo; update the upstream skill source instead.

### Available Skills

**`ref-agents-persona`** — Project persona and workflow expectations
- Use when: starting tasks, planning commits, preserving structure, or understanding communication expectations

**`ref-agents-security`** — AI policy, protected files, and exclusion sync
- Use when: changing `.ai-policy.json`, sync behavior, or generated safety files

**`ref-coding-patterns`** — Portable coding defaults across languages and CLIs
- Use when: choosing naming, typing, comments, branching structure, CLI ergonomics, or testing defaults

**`ref-python`** — Portable Python guidance for typed code, scripts, and tests
- Use when: writing or refactoring Python modules, designing Python CLIs, or deciding typing and testing patterns

**`ref-projects-architecture`** — Portable architecture guidance for feature folders and code boundaries
- Use when: deciding where code should live, splitting features, or separating product code from maintenance scripts

**`ref-skills-authoring`** — Guidelines for creating and maintaining project skills
- Use when: designing skills, updating copied skills, or evaluating skill quality

**`ref-agents-local-tasks`** — Maintain feature task tracking under `.agents/tasks/`
- Use when: a task should be tracked as a structured multi-step feature

**`tool-maintain-skills`** — Review and refresh the synced skill set after repo or workflow changes
- Use when: skills may be outdated after code or tooling changes or guidance is duplicated

## Workflow

When working on this project:

1. **Start**: Pull latest changes and rebase.
2. **Setup**: Run `uv sync` at the start of work and again after rebasing or dependency changes.
3. **Implement**: Follow the owning skill for the area you are touching.
4. **Validate**: Run lint, type-checking, and tests before committing.
5. **Commit**: Keep commits small and focused.
6. **Reflect**: Review what happened in the session, identify both corrections and durable lessons, and decide whether any skill or instruction should be updated. Summarize the result to the user and ask if they want the guidance updated. If yes, update the relevant skill using `ref-skills-authoring`, and after editing suggest a follow-up maintenance pass with `tool-maintain-skills`.

## Local Agent Workspaces

- Use `.agents/playground/` for temporary helper scripts, scratch files, and generated local artifacts that should not enter normal repo context.
- Use `.agents/tasks/` for local task tracking, task briefs, validation notes, and other ignored planning artifacts.
- Both folders are ignored by Git; do not put committed source, durable documentation, or secrets there.

## Quick Commands

- `uv sync` — Install or refresh dependencies.
- `uv run poe test` — Run tests.
- `uv run poe lint` — Check formatting.
- `uv run poe lint-fix` — Auto-format code.
- `uv run poe typecheck` — Run Pyright strict mode.
- `uv run poe lint-filter` — Run lint and filter output.
- `uv run poe typecheck-filter` — Run type-checking and filter output.
- `uv run poe sync-skills` — Sync configured shared skills into `.agents/skills/`.
- `uv run poe sync-ai-policy` — Regenerate agent config from `.ai-policy.json` through the installed `agentic-tools` package.
- `uv run poe sync-ai-policy-import-vscode` — Import VS Code approvals into policy, then sync.
- `uv run poe policy-check` — Fail when generated policy files drift from `.ai-policy.json`.

Use the Poe validation tasks above as the default way to run tests, lint, and type-checking in this repo. Only call the underlying tools directly when a task needs flags or behavior that the Poe wrapper does not expose.

## Asking for Help

- For workflow and structural caution: use `ref-agents-persona`.
- For safety config and generated policy files: use `ref-agents-security`.
- For Python structure, typing, tests, or CLI choices: use `ref-python` and `ref-coding-patterns`.
- For feature boundaries or folder decisions: use `ref-projects-architecture`.
- For task tracking under `.agents/tasks/`: use `ref-agents-local-tasks`.
- For skills themselves: use `ref-skills-authoring` and `tool-maintain-skills`.
