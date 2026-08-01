"""Seeded author pseudonyms.

Every template author is displayed under a stable adjective-animal pseudonym
derived from their anonymous install UUID, so contributors have a consistent
identity across submissions without any free-text name field (nothing to
moderate, no PII). Sponsors may replace it with a chosen display name
(template_authors.custom_name).

The derivation is HMAC-SHA256(secret, install_uuid) so pseudonyms cannot be
reversed to install UUIDs by outsiders. The secret comes from
``TESSERAE_AUTHOR_SECRET`` when set, else a random one is generated once and
persisted at ``data_dir/author_secret`` (same lifecycle as the caches; losing
it renames everyone, which is cosmetic).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
from pathlib import Path

_ADJECTIVES = (
    "amber",
    "brisk",
    "cedar",
    "coral",
    "dapper",
    "dusky",
    "ember",
    "fabled",
    "gilded",
    "hazel",
    "indigo",
    "jade",
    "keen",
    "lunar",
    "mellow",
    "misty",
    "noble",
    "ochre",
    "onyx",
    "pale",
    "quiet",
    "rustic",
    "sable",
    "saffron",
    "silver",
    "slate",
    "sunny",
    "swift",
    "tidal",
    "umber",
    "vivid",
    "wandering",
)

_ANIMALS = (
    "auk",
    "badger",
    "bittern",
    "crane",
    "curlew",
    "dormouse",
    "egret",
    "finch",
    "gannet",
    "heron",
    "ibis",
    "jackdaw",
    "kestrel",
    "lapwing",
    "marten",
    "merlin",
    "nightjar",
    "osprey",
    "otter",
    "petrel",
    "plover",
    "quail",
    "raven",
    "sandpiper",
    "shrike",
    "siskin",
    "stoat",
    "swift",
    "tern",
    "vole",
    "wagtail",
    "wren",
)


def load_or_create_secret(data_dir: Path, configured: str = "") -> str:
    """The HMAC secret: the configured value when present, else a random one
    persisted under ``data_dir`` (created on first use)."""
    if configured:
        return configured
    path = data_dir / "author_secret"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = _secrets.token_hex(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def derive(secret: str, install_uuid: str) -> str:
    """Stable ``adjective-animal-NN`` pseudonym for an install UUID."""
    digest = hmac.new(secret.encode(), install_uuid.encode(), hashlib.sha256).digest()
    adjective = _ADJECTIVES[digest[0] % len(_ADJECTIVES)]
    animal = _ANIMALS[digest[1] % len(_ANIMALS)]
    suffix = (digest[2] * 256 + digest[3]) % 100
    return f"{adjective}-{animal}-{suffix:02d}"
