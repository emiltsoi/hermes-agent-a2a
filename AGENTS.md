# HermesA2A Plugin — Agent Guidelines

**Repo:** github.com/emiltsoi/hermes-agent-a2a
**Primary owners:** Britney (execution), Linda (architecture)
**Emil:** final gate authority

---

## Gate Discipline

**Notion checkpoint is a gate criterion, not a follow-up.**

Before any gate is marked passed:
1. Notion page Status field updated
2. Notion page Notes field prepended with gate summary (what passed, key decisions, pending items)
3. Gate is only "done" when both are written

If context drops mid-gate, Emil opens the Notion tracker page and knows exactly where we stand.

**Notion tracker:** https://www.notion.so/35c296476d7381ff87eef09b8a150caa

---

## Two-Gate Review Model

Every non-trivial change goes through two sequential gates:

| Gate | Who | What |
|------|-----|------|
| Gate 1 | Britney | Self-review via `claude-code` — threading, env, subprocess, hardcoded paths |
| Gate 2 | Linda | Architect review — design integrity, SPEC.md alignment, coupling, failure modes |

If Gate 2 finds implementation bugs → Gate 1 failed. CLAUDE_PROMPT dispatch updated to catch the bug class.

---

## Test Standards

- 72 tests minimum for a full phase delivery
- Pre-existing failures must be documented with root cause and owner
- New failures introduced by a change → that change does not merge until fixed
- Test suite order sensitivity: do not reorder tests without verifying `_pending` drain behavior

---

## Publishing

- **Plugin registry:** GitHub (not ClawHub)
- **GitHub release** required for any version bump
- **Tag format:** `v{MAJOR}.{MINOR}.{PATCH}` — aligned to semantic version
- **Token scope:** GITHUB_TOKEN must have `workflow` scope to push `.github/workflows/`

---

## Key Artifacts

| Artifact | Location |
|----------|----------|
| SPEC.md | Repo root |
| Plugin source | `src/` |
| Tests | `tests/` |
| Notion tracker | Page id `35c29647-6d73-81ff-87ee-f09b8a150caa` |
| Fleet vault | `~/.hermes/fleet/a2a/` |

---

## Recovery on Context Loss

1. Open Notion tracker page — current gate status is there
2. Query Britney via A2A for latest state
3. Check `git log --oneline -3` in repo for what landed
4. Resume from last Notion checkpoint
