"""CE-026: the versioned `clip_candidates/v1` prompt and its identity.

The prompt is a packaged resource rather than a file beside the repository root.
`V0_IMPLEMENTATION_SPEC.md` illustrates it as `prompts/clip_candidates/v1.txt`
at the top level, which reads well in a source tree and does not survive
installation: nothing outside `src/content_engine` goes into the wheel, so an
installed `content-engine analyze` would have no prompt to send. It lives beside
`resources/default.toml` instead and is read through `importlib.resources`, so
it is found from a checkout, from an installed wheel, from any working
directory, and from paths containing spaces and non-ASCII characters. ADR-025
records the move.

The digest is taken over the text with line endings normalised. A `.txt` file
under `* text=auto` is checked out CRLF on Windows and LF on Linux, and hashing
the raw bytes would make the same prompt produce two different identities on the
two platforms — every manifest written here would look like a different
experiment from the identical run in CI. `.gitattributes` now pins the file to
LF as well; the normalisation is what makes the guarantee independent of a
checkout being configured correctly.
"""

from __future__ import annotations

from importlib import resources

from content_engine.domain.candidate_rules import PromptIdentity
from content_engine.domain.exceptions import ConfigurationError
from content_engine.utils.hashing import sha256_bytes

#: Traversed from the resources package rather than named as a package of its
#: own. `prompts/clip_candidates/` holds no Python and is not importable, and
#: asking `files()` for a namespace package is the kind of thing that works in a
#: source tree and fails in a zipped wheel.
PROMPT_PACKAGE = "content_engine.resources"
PROMPT_PARTS = ("prompts", "clip_candidates", "v1.txt")
PROMPT_RESOURCE = "/".join(PROMPT_PARTS)

#: The identity recorded in every manifest and artifact that used this prompt.
#: `clip_candidates/v1` is the name the roadmap and ADR-015 use; the fixture
#: analyzer's `fake-fixture/v1` is deliberately not this, so a replayed run can
#: never be mistaken for one that sent this text to a provider.
PROMPT_VERSION = "clip_candidates/v1"


def prompt_digest(text: str) -> str:
    """The identity of a prompt's content, independent of its line endings.

    Not `canonical_sha256`: that serialises a JSON payload, and this is a text
    document whose bytes are what was sent. Normalising CRLF is the only
    transformation, and it is applied to the text that is sent as well as to the
    text that is hashed, so the digest always describes the exact string the
    provider received.
    """
    return sha256_bytes(_normalise(text).encode("utf-8"))


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_prompt_text() -> str:
    """Read the packaged prompt, or fail as a configuration problem.

    An unreadable prompt is a broken installation rather than a provider
    failure, so it exits with the configuration code and says which resource is
    missing instead of raising a traceback out of the analyzer.
    """
    resource = resources.files(PROMPT_PACKAGE).joinpath(*PROMPT_PARTS)
    try:
        raw = resource.read_bytes()
    except (OSError, ModuleNotFoundError, FileNotFoundError) as error:
        raise ConfigurationError(
            f"The packaged prompt {PROMPT_PACKAGE}/{PROMPT_RESOURCE} cannot be read: "
            f"{error}. The installation is incomplete."
        ) from error
    try:
        return _normalise(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"The packaged prompt {PROMPT_PACKAGE}/{PROMPT_RESOURCE} is not valid UTF-8: {error}"
        ) from error


#: Read once at import. The resource ships inside the package and cannot change
#: while the process runs, and a prompt that failed to load should stop the
#: command rather than surface on the first call to a provider.
PROMPT_TEXT = load_prompt_text()
PROMPT_SHA256 = prompt_digest(PROMPT_TEXT)
PROMPT_IDENTITY = PromptIdentity(version=PROMPT_VERSION, sha256=PROMPT_SHA256)
