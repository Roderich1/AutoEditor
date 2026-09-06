"""CE-026: the versioned clip-candidate prompts, and which one a run selects.

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

`analysis.prompt_version` selects between them. It is a real selector rather
than a label: the version a profile names decides the resource read, the text
sent, the identity recorded, the digest, the stage configuration, the
fingerprint and therefore whether existing candidates may be reused. Before
this, the setting was inert -- a profile asking for `v2` ran `v1`, recorded
`clip_candidates/v1`, and then *silently reused* the `v1` artifacts on the next
invocation, because the stage configuration was identical either way. An
experiment could believe it had changed the prompt while changing nothing, which
is the worst failure available to a system whose whole purpose is comparing
prompts.

There is exactly one prompt today, which is the right time to fix this: the
machinery has to be correct before there are two to confuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

from content_engine.domain.candidate_rules import PromptIdentity
from content_engine.domain.exceptions import ConfigurationError
from content_engine.utils.hashing import sha256_bytes

#: Traversed from the resources package rather than named as a package of its
#: own. `prompts/clip_candidates/` holds no Python and is not importable, and
#: asking `files()` for a namespace package is the kind of thing that works in a
#: source tree and fails in a zipped wheel.
PROMPT_PACKAGE = "content_engine.resources"
PROMPT_FAMILY = "clip_candidates"

#: What `analysis.prompt_version` may name, and the resource each one selects.
#: A version is added here and nowhere else; anything absent is refused rather
#: than quietly resolved to the newest or to the only one.
PROMPT_RESOURCES: dict[str, tuple[str, ...]] = {
    "v1": ("prompts", PROMPT_FAMILY, "v1.txt"),
}

PROMPT_PARTS = PROMPT_RESOURCES["v1"]
PROMPT_RESOURCE = "/".join(PROMPT_PARTS)

#: The identity recorded in every manifest and artifact that used this prompt.
#: `clip_candidates/v1` is the name the roadmap and ADR-015 use; the fixture
#: analyzer's `fake-fixture/v1` is deliberately not this, so a replayed run can
#: never be mistaken for one that sent this text to a provider.
PROMPT_VERSION = f"{PROMPT_FAMILY}/v1"


@dataclass(frozen=True)
class Prompt:
    """One selected prompt: what was asked for, what it is, and what it says.

    ``configured`` is the short name a profile writes (`v1`); ``version`` is the
    qualified name a run records (`clip_candidates/v1`). Both are kept because a
    bare `v1` in an artifact would leave a later reader guessing which family of
    prompts it belonged to, and a qualified name in a TOML file would be
    needlessly verbose to type.
    """

    configured: str
    version: str
    sha256: str
    text: str

    @property
    def identity(self) -> PromptIdentity:
        return PromptIdentity(version=self.version, sha256=self.sha256)


def available_prompt_versions() -> tuple[str, ...]:
    """The values `analysis.prompt_version` accepts, in a stable order."""
    return tuple(sorted(PROMPT_RESOURCES))


def select_prompt(configured: str) -> Prompt:
    """Resolve `analysis.prompt_version` to a packaged prompt, or refuse.

    An unknown version is a configuration error and exits 2, before the run is
    touched. It is deliberately not resolved to the newest, or to the only one
    that exists: a profile asking for a prompt this build does not have is a
    profile whose results would be filed under the wrong name, and running the
    wrong prompt silently is precisely the failure this function exists to stop.
    """
    parts = PROMPT_RESOURCES.get(configured)
    if parts is None:
        known = ", ".join(available_prompt_versions())
        raise ConfigurationError(
            f"analysis.prompt_version is {configured!r}, which this build has no prompt "
            f"for. Available: {known}."
        )
    text = load_prompt_text(parts)
    return Prompt(
        configured=configured,
        version=f"{PROMPT_FAMILY}/{configured}",
        sha256=prompt_digest(text),
        text=text,
    )


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


def load_prompt_text(parts: tuple[str, ...] = PROMPT_PARTS) -> str:
    """Read a packaged prompt, or fail as a configuration problem.

    An unreadable prompt is a broken installation rather than a provider
    failure, so it exits with the configuration code and says which resource is
    missing instead of raising a traceback out of the analyzer.
    """
    resource = resources.files(PROMPT_PACKAGE).joinpath(*parts)
    try:
        raw = resource.read_bytes()
    except (OSError, ModuleNotFoundError) as error:
        raise ConfigurationError(
            f"The packaged prompt {PROMPT_PACKAGE}/{'/'.join(parts)} cannot be read: "
            f"{error}. The installation is incomplete."
        ) from error
    try:
        return _normalise(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"The packaged prompt {PROMPT_PACKAGE}/{'/'.join(parts)} is not valid UTF-8: {error}"
        ) from error


#: Read once at import. The resource ships inside the package and cannot change
#: while the process runs, and a prompt that failed to load should stop the
#: command rather than surface on the first call to a provider.
#:
#: These name v1 specifically. They are not "the current prompt": every code
#: path that acts on a prompt takes a selected `Prompt`, so that adding v2 can
#: never leave a caller silently pinned to this one.
PROMPT_TEXT = load_prompt_text()
PROMPT_SHA256 = prompt_digest(PROMPT_TEXT)
PROMPT_IDENTITY = PromptIdentity(version=PROMPT_VERSION, sha256=PROMPT_SHA256)
