"""One serialization for every hash the engine records.

A hash is only comparable against another hash produced the same way. The
transcription fingerprint and the stage configuration digest already shared a
private helper; V0.4 adds candidate identifiers, a chunk digest and an analysis
fingerprint, and five independent definitions of "canonical" would drift the
moment one of them was edited. They all go through here instead.

The shape is fixed deliberately:

- ``sort_keys`` so key order in the source dict cannot change the digest;
- ``separators`` without spaces so formatting cannot either;
- ``ensure_ascii=False`` so a Spanish topic hashes as its own characters rather
  than as escapes;
- ``allow_nan=False`` because ``NaN`` and the infinities are not values this
  project accepts anywhere, and a digest is the last place to start.
"""

import json
from collections.abc import Mapping
from typing import Any

from content_engine.utils.hashing import sha256_bytes


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize a payload to the one form every digest in this project uses."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """The SHA-256 of the canonical serialization, as lowercase hex."""
    return sha256_bytes(canonical_json(payload).encode("utf-8"))
