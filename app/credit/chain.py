"""Each account's postings form a hash chain: a posting's row_hash is SHA-256 of the previous
posting's row_hash and a fixed encoding of the posting and its transfer.

No key is involved, on purpose: nothing here can be lost. Someone able to write the database could
recompute a chain, so the chain's strength comes from its heads being kept outside the database
(anchors: the closings, the monthly statements, the daily anchor), which a rewritten history no
longer matches. `merkle_root` folds every account's head into the one number an anchor keeps.

ENCODING_VERSION is part of every hash. Changing what is encoded means a new version, and old
postings keep being checked with the version they were written with (today there is one).
"""

import datetime
import hashlib
import json
from collections.abc import Iterable

ENCODING_VERSION = 1
GENESIS = bytes(32)  # the prev_hash of an account's first posting


def encode(
    *,
    transfer_id: int,
    kind: str,
    created_at: datetime.datetime,
    created_by_id: int | None,
    source: str,
    dedupe_token: str,
    reverses_id: int | None,
    companion_of_id: int | None,
    account_id: int,
    counterparty_id: int,
    amount_ore: int,
    balance_after: int,
    text: str,
) -> bytes:
    """The bytes a posting's hash covers. Canonical JSON: sorted keys, no spaces, UTC to the
    microsecond, so the same row always gives the same bytes."""
    stamp = created_at.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload = {
        "v": ENCODING_VERSION,
        "transfer": {
            "id": transfer_id,
            "kind": kind,
            "created_at": stamp,
            "created_by": created_by_id,
            "source": source,
            "dedupe_token": dedupe_token,
            "reverses": reverses_id,
            "companion_of": companion_of_id,
        },
        "posting": {
            "account": account_id,
            "counterparty": counterparty_id,
            "amount": amount_ore,
            "balance_after": balance_after,
            "text": text,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def link(prev_hash: bytes, encoded: bytes) -> bytes:
    return hashlib.sha256(bytes(prev_hash) + encoded).digest()


def merkle_root(heads: Iterable[tuple[int, bytes]]) -> bytes:
    """One hash covering every (account id, chain head), in account order. An account's leaf is
    SHA-256 of its id and head, so swapping two accounts' heads changes the root. No accounts: the
    hash of nothing."""
    level = [
        hashlib.sha256(account_id.to_bytes(8, "big") + bytes(head)).digest()
        for account_id, head in sorted(heads)
    ]
    if not level:
        return hashlib.sha256(b"").digest()
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0]
