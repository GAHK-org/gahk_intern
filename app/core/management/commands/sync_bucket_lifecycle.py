"""Put the bucket's lifecycle rules into the state this project expects. Idempotent.

    THE RULES USED TO EXIST ONLY AS SHELL SOMEBODY RAN ONCE.

That is the reason this command exists. The nine rules below were applied to production by hand,
which left no way to tell whether a bucket had them, no way to give a second environment the same
ones, and no record in the repository of what the intended state even was. A rebuilt bucket would
have looked fine and quietly kept every expired object for ever.

**On a versioned bucket an `Expiration` rule frees nothing on its own.** It writes a delete marker
and the object becomes a NONCURRENT VERSION, which lives until a `NoncurrentVersionExpiration` rule
covering that prefix removes it. Every deletion rule here therefore comes in parts, and a rule set
that looks complete but has only the first part is the failure this whole module is shaped around -
it was the state `media/` had been in since the bucket was created.

**Prefix matching is literal**, so `arkiv/` does not cover `arkiv-thumb/`, and one rule with a bare
`arkiv` prefix would cover all four and overlap the zip rules, where providers differ on how
overlaps resolve. One rule per prefix, spelled out, is verbose and unambiguous.

**Merges rather than replaces.** `put_bucket_lifecycle_configuration` replaces the ENTIRE
configuration, so the naive call silently drops every rule it does not mention. Anything whose ID
this file does not claim is carried through untouched.
"""

import argparse
import json
from typing import Any

from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError

# How long a superseded or deleted object's bytes stay recoverable. The archive's originals get a
# month, because the button that destroys them says "Slet permanent" and an operator emptying a
# folder by mistake needs longer than a weekend to notice. Everything derived gets a day or a week,
# because it can simply be rebuilt.
ARCHIVE_UNDO_DAYS = 30
DERIVED_UNDO_DAYS = 1
BACKUP_UNDO_DAYS = 7

# Built zips are disposable by construction: `selection_key` names each after the selection it
# holds, so a rebuilt one lands under the same key. A week is arbitrary and safe - the cost of being
# wrong is one rebuild.
ZIP_KEEP_DAYS = 7

# Rules this project owns, by ID. Anything else in the bucket is left alone.
RULES: list[dict[str, Any]] = [
    # Bucket-wide hygiene: an upload killed half way leaves parts that are billed and invisible.
    {
        "ID": "abort-incomplete-uploads",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    },
    # --- the archive itself, and its two derived sizes -------------------------------------------
    # NO `Expiration` on any of these: nothing here expires on a clock. What they need is for a
    # deletion to actually free the bytes, which on a versioned bucket it does not.
    *(
        {
            "ID": f"expire-noncurrent-{prefix.rstrip('/')}",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "NoncurrentVersionExpiration": {"NoncurrentDays": days},
        }
        for prefix, days in (
            ("arkiv/", ARCHIVE_UNDO_DAYS),
            ("arkiv-thumb/", DERIVED_UNDO_DAYS),
            ("arkiv-preview/", DERIVED_UNDO_DAYS),
        )
    ),
    # --- built zips: the one prefix that does expire on a clock ----------------------------------
    {
        "ID": "expire-built-zips",
        "Status": "Enabled",
        "Filter": {"Prefix": "arkiv-zip/"},
        "Expiration": {"Days": ZIP_KEEP_DAYS},
        "NoncurrentVersionExpiration": {"NoncurrentDays": DERIVED_UNDO_DAYS},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    },
    # A second rule on the same prefix, because `Days` and `ExpiredObjectDeleteMarker` cannot share
    # one `Expiration` block - S3 rejects it. Without this the markers left by the rule above
    # accumulate for ever.
    {
        "ID": "expire-built-zip-markers",
        "Status": "Enabled",
        "Filter": {"Prefix": "arkiv-zip/"},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
    },
    # Photoalbum ZIP downloads are likewise disposable: the job record deletes them after seven
    # days, and this guards against a worker outage leaving an object behind indefinitely.
    {
        "ID": "expire-photo-album-zips",
        "Status": "Enabled",
        "Filter": {"Prefix": "photo-album-zips/"},
        "Expiration": {"Days": ZIP_KEEP_DAYS},
        "NoncurrentVersionExpiration": {"NoncurrentDays": DERIVED_UNDO_DAYS},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    },
    {
        "ID": "expire-photo-album-zip-markers",
        "Status": "Enabled",
        "Filter": {"Prefix": "photo-album-zips/"},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
    },
    # --- Phase 1 media ---------------------------------------------------------------------------
    {
        "ID": "expire-noncurrent-versions",
        "Status": "Enabled",
        "Filter": {"Prefix": "media/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": ARCHIVE_UNDO_DAYS},
    },
    # --- Coolify's database dumps ------------------------------------------------------------------
    # `data/coolify/…`, which is Coolify's own layout and not ours to choose (DEPLOY.md 4d).
    #
    # DELIBERATELY NO `Expiration`. Coolify decides which dumps to keep, per resource, in its own
    # UI. A rule here would be a second authority deleting backups on a different schedule, and if
    # the two ever disagreed the bucket would win silently. All this does is make Coolify's own
    # deletions actually free the bytes.
    {
        "ID": "expire-noncurrent-backups",
        "Status": "Enabled",
        "Filter": {"Prefix": "data/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": BACKUP_UNDO_DAYS},
    },
    {
        "ID": "expire-backup-markers",
        "Status": "Enabled",
        "Filter": {"Prefix": "data/"},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
    },
]

OWNED = {rule["ID"] for rule in RULES}


class Command(BaseCommand):
    help = "Sæt bucketens lifecycle-regler som projektet forventer dem. Kan køres igen."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        from botocore.exceptions import ClientError

        # Duck-typed rather than isinstance(MediaS3Storage): the check that matters is "does this
        # backend talk to a bucket", and the filesystem one used by dev and CI simply has none of
        # these attributes.
        storage = storages["default"]
        bucket = getattr(storage, "bucket_name", None)
        connection = getattr(storage, "connection", None)
        if not bucket or connection is None:
            raise CommandError(
                "No bucket configured - this needs S3_BUCKET set. On the filesystem backend used "
                "by dev and CI there is nothing to apply rules to."
            )
        client = connection.meta.client

        try:
            existing = client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        except ClientError:
            # No configuration at all yet, which is what a fresh bucket looks like.
            existing = []

        # Carried through untouched: a rule somebody added by hand for a reason this file does not
        # know about should survive a run of this command, not be quietly deleted by it.
        foreign = [rule for rule in existing if rule.get("ID") not in OWNED]
        before = {rule.get("ID") for rule in existing}

        added = sorted(OWNED - before)
        kept = sorted(OWNED & before)
        for rule_id in added:
            self.stdout.write(f"  + {rule_id}")
        for rule_id in kept:
            self.stdout.write(f"    {rule_id} (already there - replaced with the declared version)")
        for rule in foreign:
            self.stdout.write(f"    {rule.get('ID')} (not ours, left alone)")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("[dry-run] nothing written."))
            return

        client.put_bucket_lifecycle_configuration(
            Bucket=bucket, LifecycleConfiguration={"Rules": foreign + RULES}
        )
        now = client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        self.stdout.write(self.style.SUCCESS(f"{len(now)} rule(s) on {bucket}."))
        self.stdout.write(json.dumps([r.get("ID") for r in now], indent=None))
