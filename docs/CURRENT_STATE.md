# Current Project State

## Repository

- Repository: `Roderich1/AutoEditor`
- Default branch: `main`
- Merged pull requests: `#1 Proyecto base`, `#2 documentacion del proyecto base`
- Active branch: `chore/stabilize-v0-1-v0-3`
- Current package version: `0.1.0`

## Verification baseline

Last known verification (audit of `main` at `eb59fb5`):

- Ruff lint: passed
- Ruff format: **failed** on two Markdown files under `docs/`
- mypy strict: passed
- pytest: 14 tests passed
- total coverage: 69%
- real ffprobe inspection smoke test: passed
- real FFmpeg WAV extraction smoke test: passed
- real faster-whisper transcription: passed (model `tiny`, 3 s synthetic fixture,
  0 segments because the fixture carries a sine tone rather than speech)

This baseline must be updated after every milestone or PR.

## Milestone status

### V0.1 Foundation

Implemented:

- Python package and uv configuration
- Ruff, mypy and pytest
- Typer CLI
- configuration loader
- doctor command
- filesystem run workspace
- initial manifest

Pending stabilization:

- installed-wheel configuration support
- complete manifest version/hash contract
- explicit run-state transition validation
- separation between run identity and reproducible fingerprints
- additional configuration invariants

### V0.2 Media

Implemented:

- ffprobe adapter
- MediaInfo
- local input validation
- normalized WAV extraction
- manual smoke test

Pending stabilization:

- real automated FFmpeg/ffprobe integration tests
- output verification
- subprocess timeout/error hardening

### V0.3 Transcription

Implemented:

- TranscriberPort
- faster-whisper adapter
- transcript models
- JSON, TXT and SRT exports
- force option

Pending stabilization:

- transcription duration and metrics
- stronger transcript timestamp validation
- explicit rejection of incompatible transcript artifacts
- documented hardware resolution
- tests for failure paths

### V0.4 Candidate Intelligence Engine

Status: not started.

Scope:

- CE-023 through CE-033.

## Current priority

Complete one stabilization PR for V0.1-V0.3.

After stabilization is reviewed and merged, begin CE-023-CE-033 in a separate
branch and PR.

## yt-dlp decision

yt-dlp remains outside the V0 core.

It may be used manually to obtain authorized local experiment videos. It must
not be required by unit tests, integration tests or CI. A future product
integration requires a separate ADR.
