"""The declared lifecycle rule set, and the merge that must not eat anybody else's rules.

No bucket here: these assert on the DECLARATION, which is where the mistakes live. The three that
have actually bitten this project were all visible in the data - an `Expiration` with no noncurrent
half on a versioned bucket, a prefix that did not cover what it looked like it covered, and a
`put_` call that replaced the whole configuration.
"""

import pytest

from core.management.commands.sync_bucket_lifecycle import OWNED, RULES


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


def test_the_archive_prefixes_are_listed_separately() -> None:
    """Prefix matching is literal: `arkiv/` does not cover `arkiv-thumb/`. A single bare `arkiv`
    prefix would cover all four AND overlap the zip rules, where providers differ on how overlaps
    resolve."""
    prefixes = {r["Filter"].get("Prefix") for r in RULES}
    for needed in ("arkiv/", "arkiv-thumb/", "arkiv-preview/", "arkiv-zip/", "media/", "data/"):
        assert needed in prefixes, f"no rule covers {needed}"
    assert "arkiv" not in prefixes, "a bare 'arkiv' prefix would overlap every other arkiv rule"


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
