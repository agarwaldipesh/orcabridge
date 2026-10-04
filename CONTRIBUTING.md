# Contributing

Thanks for helping. Keep changes small and focused.

- **Standard library only.** The hooks run with whatever Python the installer used; do not add
  runtime dependencies.
- **Tests:** `python3 -m unittest discover tests` from the repository root. Everything is mocked;
  nothing is typed into a real terminal. Add a test for any behaviour change.
- **Lint and format:** `ruff check .` and `ruff format --check .` (config in `pyproject.toml`; CI
  pins the ruff version in `.github/workflows/ci.yml`).
- Hooks must always exit 0 and never block a tool call, a compaction or a session start.
- Never log transcript content or secrets.
