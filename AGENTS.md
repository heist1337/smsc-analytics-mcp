# Development

This repository is independently runnable with uv and FastMCP. Keep the README short.
Use `uv sync --locked`, `uv run ruff check src tests` and
`uv run python -m unittest discover -b -s tests`; build with `uv build`.
Use task branches and PRs after the initial bootstrap. Preserve unrelated work.
Never commit credentials, real env files or runtime data. Never log tokens or
provider payloads. Tests use isolated fixtures; live checks are bounded reads.
Preserve domain guards, shared budgets and uncertain-outcome handling.
Do not add Windmill, a mandatory gateway or another repository as a runtime dependency.
