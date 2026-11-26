"""The declared lifecycle rule set, and the merge that must not eat anybody else's rules.

No real bucket here: these assert on the DECLARATION, which is where the mistakes live, and the
merge runs against a stand-in. The three that have actually bitten this project were all visible in
the data - an `Expiration` with no noncurrent half on a versioned bucket, a prefix that did not
cover what it looked like it covered, and a `put_` call that replaced the whole configuration.
"""

from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest

from core.management.commands import sync_bucket_lifecycle
from core.management.commands.sync_bucket_lifecycle import ARKIV_PREFIXES, OWNED, RETIRED, RULES


def test_every_rule_has_a_unique_id() -> None:
    """IDs are what the merge keys on, so a duplicate would make one rule unreachable."""
    ids = [r["ID"] for r in RULES]
    assert len(ids) == len(set(ids)), f"duplicate rule id: {sorted(ids)}"
    assert set(ids) == OWNED


@pytest.mark.parametrize("rule", RULES, ids=lambda r: str(r["ID"]))
def test_a_clock_expiry_always_has_a_noncurrent_half(rule: dict) -> None:
    """THE BUG THIS FILE EXISTS FOR. The bucket is versioned, so `Expiration: {Days: N}` does not
    delete anything - it writes a delete marker and the bytes become a noncurrent version. A rule
    with the first half and not the second frees nothing while looking complete, which is exactly
    what `media/` did from the day the bucket was created."""
    expiration = rule.get("Expiration", {})
    if "Days" not in expiration:
        return
    assert "NoncurrentVersionExpiration" in rule, (
        f"{rule['ID']} expires objects on a clock but never expires the versions that leaves behind"
    )


def test_backups_are_never_expired_on_a_clock() -> None:
    """Coolify owns backup retention. A rule here would be a second authority deleting dumps on a
    different schedule, and the bucket would win silently."""
    for rule in RULES:
        if rule["Filter"].get("Prefix", "").startswith("data/"):
            assert "Days" not in rule.get("Expiration", {}), (
                f"{rule['ID']} would delete backups on the bucket's schedule, not Coolify's"
            )


def test_every_kept_prefix_has_a_rule() -> None:
    """Prefix matching is literal: `photo-album/` does not cover `photo-album-zips/`, so each prefix
    with a lifetime of its own needs a rule that names it exactly."""
    prefixes = {r["Filter"].get("Prefix") for r in RULES}
    for needed in ("photo-album-zips/", "media/", "data/"):
        assert needed in prefixes, f"no rule covers {needed}"


def _prefixes_the_app_writes() -> set[str]:
    """Read from the code that makes the keys, so a new prefix without its rules fails below."""
    from core.storage import MEDIA_PREFIX
    from gahkflix.models import _video_key
    from photo_album.dashboard import STORAGE_PREFIXES

    gahkflix = _video_key("uuid").split("/")[0]
    return {f"{MEDIA_PREFIX}/", *STORAGE_PREFIXES, f"{gahkflix}/"}


def test_every_prefix_the_app_writes_frees_what_it_deletes() -> None:
    """Deleting a photo, a film or an import only writes a delete marker on a versioned bucket. Each
    prefix the app writes needs a rule that expires the noncurrent versions that leaves, and one that
    clears the markers, or the bytes are billed for ever."""
    for prefix in _prefixes_the_app_writes():
        rules = [r for r in RULES if r["Filter"].get("Prefix") == prefix]
        assert any("NoncurrentVersionExpiration" in r for r in rules), f"{prefix} never frees a deleted file"
        assert any(r.get("Expiration", {}).get("ExpiredObjectDeleteMarker") for r in rules), (
            f"{prefix} keeps its delete markers for ever"
        )


def test_days_and_delete_marker_are_never_in_one_expiration() -> None:
    """S3 rejects the combination outright, so the marker cleanup has to be its own rule."""
    for rule in RULES:
        expiration = rule.get("Expiration", {})
        assert not ("Days" in expiration and "ExpiredObjectDeleteMarker" in expiration), rule["ID"]


def test_the_command_refuses_without_a_bucket() -> None:
    """Dev and CI run on the filesystem backend, where there is nothing to apply rules to. It must
    say so rather than raising an AttributeError from deep inside boto3."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="No bucket configured"):
        call_command("sync_bucket_lifecycle")


def test_a_retired_id_is_never_declared_again() -> None:
    """A retired ID that came back would be removed and re-added by the same run."""
    assert not RETIRED & OWNED


def test_every_arkiv_prefix_is_emptied_and_its_markers_cleared() -> None:
    """Arkiv is gone, so each of its prefixes expires everything (with the noncurrent half, or it
    would free nothing) and then clears the delete markers that leaves."""
    for prefix in ARKIV_PREFIXES:
        rules = [r for r in RULES if r["Filter"].get("Prefix") == prefix]
        assert any("Days" in r.get("Expiration", {}) and "NoncurrentVersionExpiration" in r for r in rules), (
            prefix
        )
        assert any(r.get("Expiration", {}).get("ExpiredObjectDeleteMarker") for r in rules), prefix


class _Bucket:
    """Just the two lifecycle calls the command makes."""

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules

    def get_bucket_lifecycle_configuration(self, Bucket: str) -> dict[str, Any]:
        return {"Rules": self.rules}

    def put_bucket_lifecycle_configuration(self, Bucket: str, LifecycleConfiguration: dict) -> None:
        self.rules = LifecycleConfiguration["Rules"]


def test_the_merge_keeps_hand_added_rules_and_removes_retired_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.core.management import call_command

    hand_added = {"ID": "somebody-elses", "Status": "Enabled", "Filter": {"Prefix": "x/"}}
    retired = {"ID": "expire-built-zips", "Status": "Enabled", "Filter": {"Prefix": "arkiv-zip/"}}
    bucket = _Bucket([hand_added, retired])
    storage = SimpleNamespace(
        bucket_name="b", connection=SimpleNamespace(meta=SimpleNamespace(client=bucket))
    )
    monkeypatch.setattr(sync_bucket_lifecycle, "storages", {"default": storage})

    call_command("sync_bucket_lifecycle", stdout=StringIO())

    ids = [r["ID"] for r in bucket.rules]
    assert "somebody-elses" in ids
    assert "expire-built-zips" not in ids
    assert set(ids) == OWNED | {"somebody-elses"}
