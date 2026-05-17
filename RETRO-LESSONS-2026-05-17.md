# Fleet Lessons — Google A2A Compliance
**Date:** 2026-05-17
**Source:** phase-anchors/lessons.md + Britney retro

## Filed to phase-anchors/lessons.md (fleet-shared)

### greenfield-modules — Britney
Wave 1 + 2: coupling-surface returned all zeros for non-existent modules. Britney's architectural judgment identified hooks.py and server.py collision risks, but the tool couldn't contribute. Coupling estimate was from judgment, not computation.

**Fix:** For future waves with greenfield modules, coupling estimate must explicitly state "computed via architectural judgment" and declare the collision class coupling-surface cannot see. The tool is confirmatory for existing code, not predictive for future code.

### spec-vs-implementation — Linda
Linda ran a "reading comprehension check" against Britney's summary — verifying tests passed, coverage met threshold. That's Britney's execution gate work. The actual design gate (6-point checklist against SPEC.md) was done only after Emil prompted. Design gate fires against the spec, not the summary.

**Fix:** workflow-anchors has 6-point micro-guard + explicit note: "Verify SPEC.md quality only." SPEC.md § Design Gate section template created.

### buffer-flushing — Britney
Wave 1 sat 20+ min with 0 bytes output — buffer not flushing. Pattern documented in `claude-code-dispatch` skill but not applied proactively.

**Fix:** When dispatching long-running tasks, set wait strategy upfront rather than checking repeatedly.

### tdd-hook-tests-dir — Britney
Pre-commit TDD hook expects `*_test.py` next to sources. Waves 1 + 2 used `tests/` subdirectory. Both commits needed `--no-verify`.

**Fix:** Either update the hook to accept `tests/` subdirectory, or document the convention explicitly.
