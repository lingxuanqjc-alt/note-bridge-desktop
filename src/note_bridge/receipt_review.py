"""Read-only receipt review bound to the two current accounts and cached versions."""

from __future__ import annotations

import json

from .errors import BridgeError
from .operations import receipt_key
from .providers.base import Provider
from .receipt_identity import FIELDS, identity_key
from .storage import Store


def review_receipts(source: Provider, target: Provider, store: Store) -> dict:
    scope = (source.spec.id, source.account_id, target.spec.id, target.account_id)
    if not source.account_id or not target.account_id:
        raise BridgeError("login_required", "请先登录迁移双方账号。")
    notes = store.notes(source.spec.id, source.account_id)
    cached_target = store.notes(target.spec.id, target.account_id)
    for provider, cached in ((source, notes), (target, cached_target)):
        if any((note.platform, note.account_id) != (provider.spec.id, provider.account_id) for note in cached):
            raise BridgeError("account_changed", "缓存与当前账号不一致，无法展示迁移记录。")
    target_ids = {note.source_id for note in cached_target}
    current = {note.source_id: note for note in notes}
    fingerprints = {note.source_id: note.fingerprint() for note in notes}
    records = []
    with store.connection() as db:
        # Each source ID is taken from its saved identity, never inferred from a
        # title or a target ID. Deleted/uncached sources can still have unknown writes.
        rows = db.execute(
            "SELECT c.*,r.status,r.remote_ids FROM receipt_contexts AS c JOIN receipts AS r ON c.key=r.key "
            "WHERE c.source_platform=? AND c.source_account=? AND c.target_platform=? AND c.target_account=? "
            "ORDER BY c.source_id,c.key", scope,
        ).fetchall()
        for row in rows:
            identity = {name: row[name] for name in FIELDS}
            if identity_key(identity) != row["key"]:
                raise BridgeError("receipt_context_mismatch", "保存的回执身份不一致，无法展示关联记录。")
            identifier = row["source_id"]
            version = ("source_missing" if identifier not in current else
                       "current" if fingerprints[identifier] == row["source_fingerprint"] else "previous")
            if version == "current" or row["status"] in ("sending", "uncertain"):
                records.append((row, identifier, version))
        # Original hash-only receipts remain reviewable only for the exact cached
        # version. Missing historical identities are never invented or backfilled here.
        for note in notes:
            row = db.execute(
                "SELECT r.* FROM receipts AS r LEFT JOIN receipt_contexts AS c ON c.key=r.key "
                "WHERE r.key=? AND c.key IS NULL", (receipt_key(note, target),),
            ).fetchone()
            if row:
                records.append((row, note.source_id, "current"))
    items = []
    for row, identifier, version in records:
        remote_ids = json.loads(row["remote_ids"])
        note = current.get(identifier)
        items.append({"id": identifier, "title": note.display_title if note else "", "version": version,
                      "status": row["status"], "remote_ids": remote_ids,
                      "cached_remote_ids": [identity for identity in remote_ids if identity in target_ids],
                      # Upload-session and object identifiers are unnecessary for a human review.
                      "resource_states": [asset["status"] for asset in store.resource_receipts(row["key"])]})
    if (source.spec.id, source.account_id, target.spec.id, target.account_id) != scope:
        raise BridgeError("account_changed", "迁移双方账号已变化，无法展示旧账号记录。")
    matched = len({item["id"] for item in items}.intersection(current))
    return {"source": source.spec.id, "target": target.spec.id, "cloud_checked": False,
            "source_cache_complete": store.snapshot(source.spec.id, source.account_id)["complete"],
            "target_cache_complete": store.snapshot(target.spec.id, target.account_id)["complete"],
            "cached_notes": len(notes), "unmatched_cached_notes": len(notes) - matched, "items": items}
