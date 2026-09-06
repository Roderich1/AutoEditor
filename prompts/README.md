# Prompts

Prompts do not live here. They are packaged resources:

```text
src/content_engine/resources/prompts/clip_candidates/v1.txt
```

`V0_IMPLEMENTATION_SPEC.md` illustrates the path as `prompts/clip_candidates/v1.txt`
at the repository root, and that location does not survive installation. Only
files under `src/content_engine` are built into the wheel, so a prompt kept here
would be missing from every installed copy of the engine and `analyze` would
have nothing to send. ADR-025 records the move and the reasoning.

A prompt is identified by a version string and the SHA-256 of its contents, both
recorded in `manifest.json` and in `analysis/config.effective.json` for every run
that used it. Editing a prompt in place therefore changes the identity of the
experiment: add a new version file rather than editing an existing one.

This directory is kept so the path in the specification resolves to this
explanation instead of to nothing.
