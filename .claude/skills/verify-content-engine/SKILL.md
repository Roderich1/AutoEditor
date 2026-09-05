# Verify Content Engine

## Verification

Run the following checks from the repository root:

```console
uv run ruff check .
uv run mypy src
uv run pytest
```

Review generated artifacts under `workspace/` and confirm no secrets are committed.
