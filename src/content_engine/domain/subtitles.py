"""Deterministic subtitles for one clip (CE-040, CE-041, CE-042).

Pure functions over domain types: no I/O, no clock, no subprocess. Given the
same words and the same interval this module produces the same two documents on
every machine, which is what makes a render reproducible rather than merely
repeatable.

**The JSON transcript stays authoritative.** SRT and ASS are exports. Neither
is read back to recover text, and where a format cannot represent something the
transcript holds, the export is the side that gives way.

**One list of cues, two renderings.** The cues are built once, in seconds, and
each format quantises them on the way out: SRT to milliseconds, ASS to
centiseconds. Building two lists would let the two documents disagree about the
same clip, which is exactly the defect nobody notices until a viewer sees a
caption a frame out of place in one player and not the other.

**Rounding is stated, not inherited.** Python's ``round`` is half-even, so
0.0005 s rounds to 0 ms and 0.0015 s to 2 ms -- correct for statistics, absurd
for timestamps, and different from what every other subtitle tool does. Both
quantisers here use ``Decimal`` with ``ROUND_HALF_UP``, and both are clamped to
the clip so a rounded-up end cannot reach past the last frame.

**Nothing after rounding can be empty or out of order.** Cues are given a
minimum visible duration before quantisation, so the smallest cue is 250 ms --
25 centiseconds -- and no rounding step can collapse one to zero. Quantisation
is monotone, so an ordered list of cues stays ordered.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite

from pydantic import Field, model_validator

from content_engine.domain.candidates import _Artifact
from content_engine.domain.models import TranscriptWord

__all__ = [
    "ASS_STYLE_NAME",
    "DEFAULT_ASS_STYLE",
    "DEFAULT_SUBTITLE_RULES",
    "SUBTITLE_RULES_VERSION",
    "AssStyle",
    "SubtitleCue",
    "SubtitleEvent",
    "SubtitleRules",
    "ass_text",
    "ass_timestamp",
    "build_cues",
    "read_ass_events",
    "read_srt_events",
    "render_ass",
    "render_srt",
    "srt_timestamp",
    "to_centiseconds",
    "to_milliseconds",
]

#: Bumped whenever the grouping, clamping, rounding or escaping rules change.
#: They decide what the two documents contain, so a change to any of them has to
#: invalidate every render fingerprint that claimed the previous behaviour.
SUBTITLE_RULES_VERSION = 1

#: How much slack a comparison against a clip boundary is given. The same value
#: the candidate engine uses, and for the same reason: these are binary floats
#: and a word clamped to an interval is equal to that interval only to within
#: representation error.
_EPSILON = 1e-9

#: A word ending on one of these has finished a thought, so the cue ends there
#: however few words it holds. Spanish opens with the inverted marks and closes
#: with the same ones every other language uses.
_SENTENCE_END = frozenset(".?!…")
#: A weaker break, honoured only once a cue already holds enough words to be
#: worth ending. Breaking on the first comma would produce two-word cues.
_SOFT_BREAK = frozenset(",;:")
#: Trailing characters that close a quotation or a parenthesis after the mark
#: that actually ended the sentence, and must not hide it.
_CLOSING = "\"')]»”"


@dataclass(frozen=True)
class SubtitleRules:
    """What a readable cue is, as numbers rather than as judgement.

    A frozen dataclass rather than configuration: these shape what the exported
    files contain, they are recorded in full in the stage configuration, and
    ADR-028's reasoning applies unchanged -- folding them into ``config_sha256``
    would change the logical identity of every existing experiment to describe a
    subtitle layout.
    """

    max_words_per_cue: int
    min_words_before_soft_break: int
    max_lines: int
    max_chars_per_line: int
    pause_seconds: float
    min_cue_seconds: float

    def __post_init__(self) -> None:
        for name in ("max_words_per_cue", "max_lines", "max_chars_per_line"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")
        if self.min_words_before_soft_break < 1:
            raise ValueError(
                f"min_words_before_soft_break must be at least 1, got "
                f"{self.min_words_before_soft_break}"
            )
        for name in ("pause_seconds", "min_cue_seconds"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number, got {value}")

    @property
    def max_chars_per_cue(self) -> int:
        return self.max_lines * self.max_chars_per_line


#: The baseline the specification asks for: around six to eight visible words,
#: at most two lines, broken at punctuation and pauses where there is one.
DEFAULT_SUBTITLE_RULES = SubtitleRules(
    max_words_per_cue=8,
    min_words_before_soft_break=6,
    max_lines=2,
    max_chars_per_line=42,
    pause_seconds=0.6,
    min_cue_seconds=0.25,
)


class SubtitleCue(_Artifact):
    """One caption, in clip-local seconds, already broken into its lines.

    The lines are decided here rather than by the renderer, because the two
    renderers must break the text in the same place: a viewer comparing the
    burned-in caption with the sidecar SRT is comparing two files this module
    promised were the same.
    """

    index: int = Field(ge=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    lines: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cue(self) -> SubtitleCue:
        if self.end <= self.start:
            raise ValueError(f"cue {self.index} ends at {self.end}, at or before its start")
        for line in self.lines:
            if not line.strip():
                raise ValueError(f"cue {self.index} holds a blank line")
            if line != " ".join(line.split()):
                raise ValueError(
                    f"cue {self.index} holds a line break or repeated whitespace: {line!r}"
                )
        return self

    @property
    def text(self) -> str:
        """The cue as one line, which is what a grouping assertion is about."""
        return " ".join(self.lines)


@dataclass(frozen=True)
class SubtitleEvent:
    """One event read back out of a finished document (CE-046).

    Times are integer milliseconds because that is the coarsest unit either
    format carries, so an SRT event and the ASS event beside it are comparable
    without a float ever entering the verification.
    """

    start_ms: int
    end_ms: int
    text: str


# --- building ----------------------------------------------------------------


def _require_interval(start: float, end: float) -> None:
    for name, value in (("start", start), ("end", end)):
        if not isfinite(value):
            raise ValueError(f"clip {name} must be a finite number, got {value}")
    if start < 0:
        raise ValueError(f"clip start is negative ({start})")
    if end <= start:
        raise ValueError(f"clip ends at {end}, at or before its start ({start})")


def _ends_with(text: str, marks: frozenset[str]) -> bool:
    stripped = text.rstrip(_CLOSING)
    return bool(stripped) and stripped[-1] in marks


@dataclass(frozen=True)
class _Token:
    """A word already clamped to the clip and expressed in clip-local time."""

    text: str
    start: float
    end: float


def _tokens(words: Sequence[TranscriptWord], clip_start: float, clip_end: float) -> list[_Token]:
    """Select, clamp and rebase the words this clip actually shows.

    Intersection is strict: a word ending exactly at the clip start or beginning
    exactly at its end shares no time with the clip, and including it would put
    a caption on screen for material the viewer cannot hear.

    Whitespace inside a word is collapsed, which is not cosmetic. It is what
    guarantees that no cue text can hold a line break -- and a line break inside
    a cue would terminate it early in SRT and split the Dialogue line in ASS.
    """
    selected = [
        candidate
        for candidate in words
        if candidate.end - clip_start > _EPSILON and clip_end - candidate.start > _EPSILON
    ]
    # Sorted rather than trusted. Words are ordered within a segment and the
    # segments are ordered by start, but nothing forbids two adjacent segments
    # from overlapping, and a cue built from unordered words is a caption that
    # goes backwards.
    selected.sort(key=lambda candidate: (candidate.start, candidate.end))
    tokens = []
    for candidate in selected:
        text = " ".join(candidate.word.split())
        if not text:
            continue
        start = max(candidate.start, clip_start) - clip_start
        end = min(candidate.end, clip_end) - clip_start
        tokens.append(_Token(text=text, start=start, end=max(end, start)))
    return tokens


def _group(tokens: list[_Token], rules: SubtitleRules) -> list[list[_Token]]:
    """Split the words into cues, greedily and in one pass.

    Greedy rather than optimal on purpose. An optimiser would produce prettier
    line breaks and would also make the output depend on a search whose result
    changes whenever the cost function is touched; the point of this module is
    that the same words always give the same file.
    """
    groups: list[list[_Token]] = []
    current: list[_Token] = []
    for token in tokens:
        if current and _breaks_before(current, token, rules):
            groups.append(current)
            current = []
        current.append(token)
    if current:
        groups.append(current)
    return groups


def _breaks_before(current: list[_Token], token: _Token, rules: SubtitleRules) -> bool:
    previous = current[-1]
    if len(current) >= rules.max_words_per_cue:
        return True
    if _ends_with(previous.text, _SENTENCE_END):
        return True
    if token.start - previous.end >= rules.pause_seconds:
        return True
    if len(current) >= rules.min_words_before_soft_break and _ends_with(previous.text, _SOFT_BREAK):
        return True
    length = sum(len(item.text) for item in current) + len(current) + len(token.text)
    return length > rules.max_chars_per_cue


def _wrap(words: list[str], rules: SubtitleRules) -> list[str]:
    """Break one cue into at most ``max_lines`` lines, as evenly as possible.

    A word is never split. A single word longer than the line budget overflows
    its line instead, because a caption reading ``systemct`` / ``l`` is worse
    than one that is slightly too wide.
    """
    joined = " ".join(words)
    if len(joined) <= rules.max_chars_per_line or len(words) == 1 or rules.max_lines == 1:
        return [joined]
    best: tuple[int, int] | None = None
    for split in range(1, len(words)):
        first = " ".join(words[:split])
        second = " ".join(words[split:])
        cost = max(len(first), len(second))
        if best is None or cost < best[0]:
            best = (cost, split)
    # Unreachable with two or more words, kept explicit rather than asserted.
    if best is None:  # pragma: no cover - defensive
        return [joined]
    return [" ".join(words[: best[1]]), " ".join(words[best[1] :])]


def _timed(
    groups: list[list[_Token]], clip_duration: float, rules: SubtitleRules
) -> list[tuple[float, float, list[_Token]]]:
    """Give every group an interval that is ordered, visible and inside the clip.

    Three corrections, in this order, and the order is what makes them
    composable: overlap is removed first so a cue can only ever be extended into
    free time, then the extension is bounded by whichever comes first -- the
    next cue or the end of the clip -- and a cue with nowhere to grow is
    dropped rather than written with zero duration, because a zero-duration
    event is a caption no player shows and every validator complains about.
    """
    intervals = [
        (min(item.start for item in group), max(item.end for item in group), group)
        for group in groups
    ]
    resolved: list[tuple[float, float, list[_Token]]] = []
    previous_end = 0.0
    for position, (start, end, group) in enumerate(intervals):
        start = min(max(start, previous_end), clip_duration)
        end = min(max(end, start), clip_duration)
        limit = intervals[position + 1][0] if position + 1 < len(intervals) else clip_duration
        limit = min(max(limit, start), clip_duration)
        if end - start < rules.min_cue_seconds:
            end = min(start + rules.min_cue_seconds, limit)
        if end - start <= _EPSILON:
            continue
        resolved.append((start, end, group))
        previous_end = end
    return resolved


def build_cues(
    words: Sequence[TranscriptWord],
    clip_start: float,
    clip_end: float,
    rules: SubtitleRules = DEFAULT_SUBTITLE_RULES,
) -> list[SubtitleCue]:
    """CE-040. Absolute transcript words become clip-local cues.

    ``local = absolute - clip_start`` is the whole conversion, and everything
    around it exists to make sure that subtraction is applied to a word that
    really belongs to this clip and produces a time inside it.

    An interval with no words in it returns no cues. That is silence, not a
    failure: a clip of a command running with nothing said over it is a real
    thing to render, and refusing it would make the subtitle rules decide which
    moments a person is allowed to publish.
    """
    _require_interval(clip_start, clip_end)
    duration = clip_end - clip_start
    tokens = _tokens(words, clip_start, clip_end)
    if not tokens:
        return []
    timed = _timed(_group(tokens, rules), duration, rules)
    return [
        SubtitleCue(
            index=number,
            start=start,
            end=end,
            lines=_wrap([item.text for item in group], rules),
        )
        for number, (start, end, group) in enumerate(timed, start=1)
    ]


# --- quantisation ------------------------------------------------------------


def _quantise(seconds: float, units_per_second: int) -> int:
    """Round to whole units, half away from zero, never below zero.

    ``Decimal`` rather than arithmetic on the float: ``int(x * 1000 + 0.5)``
    gets 1.0005 wrong on a binary float, and this value ends up in a timestamp
    a viewer compares against a picture.
    """
    if not isfinite(seconds):
        raise ValueError(f"a timestamp must be a finite number, got {seconds}")
    scaled = Decimal(repr(float(seconds))) * units_per_second
    return max(0, int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def to_milliseconds(seconds: float) -> int:
    """Clip-local seconds as whole milliseconds, for SRT."""
    return _quantise(seconds, 1000)


def to_centiseconds(seconds: float) -> int:
    """Clip-local seconds as whole centiseconds, for ASS."""
    return _quantise(seconds, 100)


def srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(int(milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def ass_timestamp(centiseconds: int) -> str:
    """``H:MM:SS.cc``: one hour digit, which is what the format specifies."""
    hours, remainder = divmod(int(centiseconds), 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{hundredths:02d}"


# --- SRT ---------------------------------------------------------------------


def render_srt(cues: Sequence[SubtitleCue]) -> str:
    """CE-041. The cues as an SRT document, numbered from 1.

    UTF-8 without a BOM and LF endings are the caller's business -- every
    artifact in this engine is written that way -- but the string produced here
    contains no carriage return, so writing it with ``newline="\\n"`` is enough.
    """
    blocks = [
        "\n".join(
            (
                str(number),
                f"{srt_timestamp(to_milliseconds(cue.start))} --> "
                f"{srt_timestamp(to_milliseconds(cue.end))}",
                *cue.lines,
            )
        )
        for number, cue in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


_SRT_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$")


def read_srt_events(text: str) -> list[SubtitleEvent]:
    """Parse a finished SRT back, or refuse it.

    Used by verification rather than by the pipeline: CE-046 asks whether the
    file on disk is a subtitle document a player will accept, and the only
    honest way to answer is to read it as one.
    """
    events: list[SubtitleEvent] = []
    blocks = [block for block in text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    for position, block in enumerate(blocks, start=1):
        lines = [line for line in block.split("\n") if line.strip() or events]
        lines = block.strip("\n").split("\n")
        if len(lines) < 3:
            raise ValueError(f"SRT cue {position} has no text: {block!r}")
        if lines[0].strip() != str(position):
            raise ValueError(f"SRT cue {position} is numbered {lines[0]!r}")
        match = _SRT_TIME.match(lines[1].strip())
        if match is None:
            raise ValueError(f"SRT cue {position} has no valid timestamps: {lines[1]!r}")
        numbers = [int(value) for value in match.groups()]
        start = ((numbers[0] * 60 + numbers[1]) * 60 + numbers[2]) * 1000 + numbers[3]
        end = ((numbers[4] * 60 + numbers[5]) * 60 + numbers[6]) * 1000 + numbers[7]
        if end <= start:
            raise ValueError(f"SRT cue {position} ends at or before it starts")
        events.append(SubtitleEvent(start_ms=start, end_ms=end, text="\n".join(lines[2:])))
    return events


# --- ASS ---------------------------------------------------------------------

#: The only style the engine writes. Named once so the Dialogue lines and the
#: style definition cannot drift apart.
ASS_STYLE_NAME = "ContentEngine"

#: The Format line of the [V4+ Styles] section, in the order libass expects.
_STYLE_FORMAT = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
    "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
_EVENT_FORMAT = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


@dataclass(frozen=True)
class AssStyle:
    """How the burned-in caption looks.

    A stage constant like the encoder settings, recorded in full in the stage
    configuration. Colours are ASS ``&HAABBGGRR`` literals, which is the format
    reversed relative to every other tool and therefore worth writing down.
    """

    font_name: str
    font_size: int
    primary_colour: str
    outline_colour: str
    back_colour: str
    bold: bool
    outline: float
    shadow: float
    #: Numpad convention: 2 is bottom-centre.
    alignment: int
    margin_horizontal: int
    #: Distance from the bottom edge. Large on purpose: a vertical video has
    #: player chrome and a caption at the very bottom is under it.
    margin_vertical: int


DEFAULT_ASS_STYLE = AssStyle(
    font_name="Arial",
    font_size=64,
    primary_colour="&H00FFFFFF",
    outline_colour="&H00000000",
    back_colour="&HA0000000",
    bold=True,
    outline=4.0,
    shadow=0.0,
    alignment=2,
    margin_horizontal=80,
    margin_vertical=320,
)


def ass_text(text: str) -> str:
    """Make one line safe to put in a Dialogue Text field.

    ASS gives three characters structural meaning inside that field: ``{`` and
    ``}`` delimit an override block, whose contents libass interprets as
    formatting commands and then removes from the picture, and ``\\`` begins an
    escape, of which ``\\N`` is a hard line break.

    None of the three has a portable escape. The format defines no ``\\{``, and
    libass and VSFilter disagree about what an unmatched brace does, so a
    transcript that happened to contain one would render differently depending
    on the player -- or silently lose the words after it. They are transliterated
    instead, under ``SUBTITLE_RULES_VERSION``: a brace becomes a parenthesis and
    a backslash becomes a forward slash.

    That is lossy, and it is the smaller cost. The SRT export and
    ``transcript.json`` keep the exact characters, and the JSON is the artifact
    every measurement reads.
    """
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def render_ass(
    cues: Sequence[SubtitleCue],
    width: int,
    height: int,
    style: AssStyle = DEFAULT_ASS_STYLE,
) -> str:
    """CE-042. The cues as an ASS document sized for this clip.

    ``PlayResX``/``PlayResY`` are the clip's own dimensions, so the font size
    and margins below are in output pixels rather than in a coordinate system
    libass would have to scale. ``WrapStyle: 2`` disables automatic wrapping:
    the lines were already chosen by ``build_cues``, and letting the renderer
    re-wrap them would put the burned-in caption and the sidecar SRT on
    different line breaks.
    """
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        f"Format: {_STYLE_FORMAT}",
        "Style: "
        + ",".join(
            (
                ASS_STYLE_NAME,
                style.font_name,
                str(style.font_size),
                style.primary_colour,
                style.primary_colour,
                style.outline_colour,
                style.back_colour,
                "-1" if style.bold else "0",
                "0",
                "0",
                "0",
                "100",
                "100",
                "0",
                "0",
                "1",
                _number(style.outline),
                _number(style.shadow),
                str(style.alignment),
                str(style.margin_horizontal),
                str(style.margin_horizontal),
                str(style.margin_vertical),
                "1",
            )
        ),
        "",
        "[Events]",
        f"Format: {_EVENT_FORMAT}",
    ]
    dialogue = [
        "Dialogue: "
        + ",".join(
            (
                "0",
                ass_timestamp(to_centiseconds(cue.start)),
                ass_timestamp(to_centiseconds(cue.end)),
                ASS_STYLE_NAME,
                "",
                "0",
                "0",
                "0",
                "",
                "\\N".join(ass_text(line) for line in cue.lines),
            )
        )
        for cue in cues
    ]
    return "\n".join((*header, *dialogue)) + "\n"


def _number(value: float) -> str:
    """Write a style number without a trailing ``.0`` where it is a whole one."""
    return str(int(value)) if float(value).is_integer() else str(value)


_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")


def read_ass_events(text: str) -> list[SubtitleEvent]:
    """Parse the Dialogue lines of a finished ASS back, or refuse it.

    Only the fields verification needs are read. The Text field is last, so it
    is split off with a bounded ``split`` and keeps any comma it contains, which
    is why the format puts it there.
    """
    events: list[SubtitleEvent] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.removeprefix("Dialogue:").split(",", 9)
        if len(fields) != 10:
            raise ValueError(f"ASS dialogue line has {len(fields)} fields, expected 10: {line!r}")
        start = _ass_milliseconds(fields[1].strip(), line)
        end = _ass_milliseconds(fields[2].strip(), line)
        if end <= start:
            raise ValueError(f"ASS dialogue line ends at or before it starts: {line!r}")
        events.append(
            SubtitleEvent(start_ms=start, end_ms=end, text=fields[9].replace("\\N", "\n"))
        )
    return events


def _ass_milliseconds(value: str, line: str) -> int:
    match = _ASS_TIME.match(value)
    if match is None:
        raise ValueError(f"ASS dialogue line has no valid timestamp {value!r}: {line!r}")
    hours, minutes, seconds, hundredths = (int(part) for part in match.groups())
    return (((hours * 60 + minutes) * 60) + seconds) * 1000 + hundredths * 10
