# Global default prompts

Intentionally empty. Each project keeps its own prompts in
`<project>/.loop/agents/<agent>/<phase>.md`; this directory is only the fallback.

Lookup order for agent `<agent>`, phase `<phase>`:

1. `<project>/.loop/agents/<agent>/<phase>.md`
2. `<this dir>/<agent>/<phase>.md`

If neither exists, `run` fails with exit code 1 and names the missing file.
Put a prompt here only if it is truly project-agnostic. Otherwise new projects
silently inherit rules written for another project.

Expected files: `agy/01_planning.md`, `claude/02_implement.md`,
`codex/03_qa_review.md`, `codex/04_refactor.md`.
Placeholders: `{{WORKSPACE}}` (project path), `{{MEDIA}}` (media path, if registered).
