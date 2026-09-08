"""Receipt version keys and stable migration identity; never includes credentials."""
from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from .models import NoteDocument, PlatformId

if TYPE_CHECKING:
    from .providers.base import Provider

FIELDS = ("source_platform", "source_account", "source_id", "source_fingerprint", "target_platform", "target_account")
SCOPE_FIELDS = ("source_platform", "source_account", "source_id", "target_platform", "target_account")


def migration_identity(note: NoteDocument, target: Provider) -> dict[str, str]:
    return dict(zip(FIELDS, (note.platform, note.account_id, note.source_id, note.fingerprint(),
                             target.spec.id, target.account_id), strict=True))


def identity_key(identity: dict[str, str]) -> str:
    # Keep the exact original serialization so existing receipt keys remain valid.
    if (not isinstance(identity, dict) or set(identity) != set(FIELDS)
            or any(not isinstance(identity[name], str) or not identity[name] for name in FIELDS)
            or not re.fullmatch(r"[0-9a-f]{64}", identity["source_fingerprint"])):
        raise ValueError("Incomplete migration identity")
    PlatformId(identity["source_platform"])
    PlatformId(identity["target_platform"])
    return hashlib.sha256(json.dumps([identity[name] for name in FIELDS], ensure_ascii=False).encode()).hexdigest()


def receipt_key(note: NoteDocument, target: Provider) -> str:
    return identity_key(migration_identity(note, target))
