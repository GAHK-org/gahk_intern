"""Postkasser — the embedsgruppers shared one.com mailboxes (ak@, bierkeller@, festgruppen@ …), read
and answered from inside the intranet instead of one.com's webmail.

one.com has no API, so the only way in is the mailbox's own IMAP/SMTP login. That shapes the model:

  * `Mailbox` holds the login. The password is stored ENCRYPTED (postkasser.crypto) and only ever
    decrypted server-side for an IMAP/SMTP connection — no resident sees it, which is the whole point:
    a group no longer passes one shared password from hand to hand each month.
  * Who may open a mailbox is decided by the group, not by a person: membership of one of
    `workgroups` in the active month (the embedsgruppe a resident is on this månedsliste), or holding
    one of `roles`. Both rotate on their own as the lists change — see access.can_open.
  * `MailFolder` mirrors the folders one.com has (Indbakke, Sendt, whatever the group made). They are a
    FILTER on the board, never a place a card lives: moving a card changes nothing at one.com. Moving
    the MAIL to another folder is a separate act (services.move_messages) that does happen at one.com.
  * `MailMessage` is a HEADER cache only (sender, subject, date, flags), filled by tasks.sync_mailboxes
    so the board does not wait on one.com. Bodies and attachments are fetched live from IMAP when
    a message is opened and never stored here: the mail lives at one.com, and this database does not
    become a second copy of every group's correspondence.
  * `MailThread` is a "sag": the mails that belong together (by Message-ID / In-Reply-To / References),
    one card on the board. Its status is OUR OWN — the `BoardColumn` it sits in — and lives only here.
  * `BoardColumn` is a column of a mailbox's board. Every group names and orders its own, so nothing
    in the code ever looks at a NAME: automation goes by `role` (see BoardColumn).
  * `MailTag` is a category a group sticks on sager, per mailbox, intranet-only.
  * `MailDraft` is a half-written mail (kladde), kept in THIS database only: nothing goes to one.com's
    Kladder folder, and nothing is sent until "Send". The group can see that a draft exists on a sag (and
    read it), so two people do not answer at once; only its author can change, send or delete it.
  * `MailEvent` is a small log — who read, answered, moved or handed over what — because several
    people share one inbox and "has anyone answered this?" is the question a shared inbox has to answer.
"""

from datetime import timedelta
from typing import Any

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Workgroup
from residents.models import Resident, Role


class Mailbox(models.Model):
    name = models.CharField("Navn", max_length=100, help_text="Vises i menuen og som afsendernavn.")
    slug = models.SlugField(unique=True, help_text="Bruges i adressen, f.eks. /postkasser/ak/.")
    address = models.EmailField("Mailadresse", unique=True)
    username = models.CharField(
        "Brugernavn", max_length=254, blank=True, help_text="Lad stå tomt for at bruge mailadressen."
    )
    # Fernet token (postkasser.crypto). editable=False: the admin sets it through a write-only form
    # field (admin.MailboxForm.password), never by showing or round-tripping the ciphertext.
    password_encrypted = models.TextField(editable=False, blank=True)
    imap_host = models.CharField(max_length=255, default="imap.one.com")
    imap_port = models.PositiveIntegerField(default=993)
    smtp_host = models.CharField(max_length=255, default="send.one.com")
    smtp_port = models.PositiveIntegerField(default=465, help_text="465 = SSL, 587 = STARTTLS.")
    # Where a sent reply is filed (IMAP APPEND), so it shows up in one.com's "Sendt" too. A folder
    # name, not a flag, because providers name it differently — `manage.py sync_mailboxes --folders`
    # lists what the server actually has.
    # "" = automatic: the folder the server marks as Sent (services.sent_folder_name).
    sent_folder = models.CharField(
        "Mappe til sendt post",
        max_length=100,
        blank=True,
        default="",
        help_text="Automatisk bruger serverens egen Sendt-mappe.",
    )
    signature = models.TextField("Signatur", blank=True, help_text="Sættes under hver mail der sendes.")

    workgroups = models.ManyToManyField(
        Workgroup,
        blank=True,
        verbose_name="Embedsgrupper",
        help_text="Beboere på denne embedsgruppe i den aktive måned har adgang.",
    )
    roles = models.JSONField(
        "Roller",
        default=list,
        blank=True,
        help_text='Rollekoder med adgang, f.eks. ["ak"]. Administratorer har alle roller.',
    )
    is_active = models.BooleanField("Aktiv", default=True)

    # Sync bookkeeping (tasks.sync_mailboxes). UIDVALIDITY, IMAP's "the UIDs you cached no longer mean
    # anything" signal, is per FOLDER — see MailFolder.uidvalidity.
    last_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_sync_error = models.TextField(blank=True, editable=False)
    # Set while a sync or a move runs on this mailbox, claimed with a compare-and-swap UPDATE in the
    # database (services.claim) — the one lock that works between a Celery worker and a web request.
    # A claim older than services.CLAIM_TTL is a crashed run's and may be taken over.
    sync_started_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["name"]
        verbose_name = "postkasse"
        verbose_name_plural = "postkasser"

    def __str__(self) -> str:
        return f"{self.name} <{self.address}>"

    # Written only by the sync (services.claim / sync / the sync command), always with `.update()` or
    # `update_fields`. See save().
    BOOKKEEPING_FIELDS = frozenset({"sync_started_at", "last_synced_at", "last_sync_error"})

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """A plain `save()` of an existing row (the admin, a form) writes everything BUT the sync
        bookkeeping. Those fields are loaded when the form opens and would be written back stale:
        releasing a live claim (two syncs at once), or setting a dead one (a lockout), or reverting
        the sync status. Whoever really sets them passes `update_fields` (or uses `.update()`)."""
        if not self._state.adding and len(args) < 4 and kwargs.get("update_fields") is None:
            kwargs["update_fields"] = [
                f.name
                for f in self._meta.concrete_fields
                if not f.primary_key and f.name not in self.BOOKKEEPING_FIELDS
            ]
        super().save(*args, **kwargs)

    @property
    def login(self) -> str:
        return self.username or self.address

    def clean_roles(self) -> list[str]:
        """`roles` filtered to codes that exist — a typo in the admin grants nothing rather than
        erroring at request time."""
        return [r for r in self.roles or [] if r in Role.values]


class MailFolder(models.Model):
    """A folder as one.com has it, refreshed on every sync (services.sync)."""

    class SpecialUse(models.TextChoices):
        NONE = "", "Ingen"
        INBOX = "inbox", "Indbakke"
        SENT = "sent", "Sendt"
        DRAFTS = "drafts", "Kladder"
        TRASH = "trash", "Papirkurv"
        JUNK = "junk", "Spam"
        ARCHIVE = "archive", "Arkiv"

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="folders")
    # As the server knows it (modified UTF-7, see utf7.py) — what SELECT has to be sent.
    name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)  # decoded, last path segment
    path = models.CharField(max_length=500)  # decoded full path, "/"-separated: sorting and nesting
    depth = models.PositiveSmallIntegerField(default=0)
    delimiter = models.CharField(max_length=5, blank=True)
    special_use = models.CharField(max_length=10, choices=SpecialUse.choices, blank=True)
    selectable = models.BooleanField(default=True)
    uidvalidity = models.BigIntegerField(null=True, blank=True, editable=False)
    last_synced_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["path"]
        constraints = [models.UniqueConstraint(fields=["mailbox", "name"], name="postkasser_unique_folder")]

    def __str__(self) -> str:
        return self.display_name


class ColumnRole(models.TextChoices):
    """What the automation means by a column, whatever the group has called it."""

    NONE = "", "Ingen"
    NY = "ny", "Indgående"
    AFVENTER = "afventer", "Afventer svar"
    FAERDIG = "faerdig", "Færdig"


# What every mailbox starts with: (name, role). Position is the order here.
DEFAULT_COLUMNS = [
    ("Ny", ColumnRole.NY),
    ("I gang", ColumnRole.NONE),
    ("Afventer svar", ColumnRole.AFVENTER),
    ("Færdig", ColumnRole.FAERDIG),
]


class BoardColumn(models.Model):
    """One column of a mailbox's board, named and ordered by the group itself.

    Code never matches on `name`; it asks for the column holding a ROLE (columns.for_role):
      * `ny` — where a new sag lands, and where one goes back when a customer writes again;
      * `afventer` — where a sag goes once we have answered (optional: a group may not want one);
      * `faerdig` — done; the board shows these for 30 days, after which they live in Arkiv.
    Every mailbox always has exactly one `ny` and one `faerdig`, at most one `afventer`, and a role is
    held by one column at most (the constraint below). columns.py is where that is kept true.
    """

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="columns")
    name = models.CharField("Navn", max_length=60)
    position = models.PositiveIntegerField("Placering", default=0)
    role = models.CharField("Funktion", max_length=10, choices=ColumnRole.choices, blank=True)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["mailbox", "name"], name="postkasser_unique_column_name"),
            # One column per role — but any number of role-less ones, hence the condition.
            models.UniqueConstraint(
                fields=["mailbox", "role"],
                condition=~models.Q(role=""),
                name="postkasser_unique_column_role",
            ),
        ]
        verbose_name = "kolonne"
        verbose_name_plural = "kolonner"

    def __str__(self) -> str:
        return self.name


def create_default_columns(mailbox: Mailbox) -> None:
    BoardColumn.objects.bulk_create(
        BoardColumn(mailbox=mailbox, name=name, role=role, position=position)
        for position, (name, role) in enumerate(DEFAULT_COLUMNS)
    )


@receiver(post_save, sender=Mailbox)
def _default_columns_for_new_mailbox(
    sender: type[Mailbox], instance: Mailbox, created: bool, raw: bool, **kwargs: object
) -> None:
    if created and not raw:
        create_default_columns(instance)


class TagColor(models.TextChoices):
    """A fixed palette: each is a light background with a dark ink of its own (styles.css, `.chip-tag-*`),
    so a tag reads the same wherever it is shown. The value is the CSS class suffix."""

    GRAA = "graa", "Grå"
    ROED = "roed", "Rød"
    ORANGE = "orange", "Orange"
    GUL = "gul", "Gul"
    GROEN = "groen", "Grøn"
    BLAA = "blaa", "Blå"
    LILLA = "lilla", "Lilla"
    PINK = "pink", "Pink"


class MailTag(models.Model):
    """A category a group sticks on its sager ("Faktura", "Brygger"), kept per mailbox like the columns.
    Intranet-only: nothing is written to one.com (its webmail would not show IMAP keywords anyway).
    Names are unique per mailbox ignoring case — checked in tags.py, since the database's own
    case-insensitive match only folds ASCII on SQLite and these are Danish names."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField("Navn", max_length=40)
    color = models.CharField("Farve", max_length=10, choices=TagColor.choices, default=TagColor.GRAA)
    position = models.PositiveIntegerField("Placering", default=0)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [models.UniqueConstraint(fields=["mailbox", "name"], name="postkasser_unique_tag_name")]
        verbose_name = "tag"
        verbose_name_plural = "tags"

    def __str__(self) -> str:
        return self.name


class MailThread(models.Model):
    """A sag: the mails that belong together, one card on the board."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="threads")
    subject = models.CharField(max_length=998, blank=True)
    # RESTRICT, not PROTECT: a column that still holds sager cannot be deleted by itself
    # (columns.delete_column moves them first), but deleting the whole mailbox — which cascades to
    # both its columns and its threads — must still work, and PROTECT would refuse that too.
    column = models.ForeignKey(BoardColumn, on_delete=models.RESTRICT, related_name="threads")
    assignee = models.ForeignKey(Resident, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    last_message_at = models.DateTimeField(null=True, blank=True)
    # Categories (MailTag) belong to the SAG, not to a mail: the card is the unit on the board, and a
    # category should survive the next reply.
    tags = models.ManyToManyField(MailTag, blank=True, related_name="threads")
    # The Message-ID of a mail sent from the intranet, until its copy turns up in the sent folder
    # and joins this sag (services.sync).
    pending_message_id = models.CharField(max_length=998, blank=True)
    # Set by a sync that found the sag with no mail left while some folder could not be read — the mail
    # may be sitting in that folder. The sag is hidden, not deleted, until a run where every folder
    # answers (or EMPTY_THREAD_TTL passes); `emptied_ids` are the Message-IDs it lost, so the mail can
    # find its way back to it in a later run (services._lookup_thread). Cleared when a mail joins.
    emptied_at = models.DateTimeField(null=True, blank=True, editable=False)
    emptied_ids = models.TextField(blank=True, editable=False)
    # When the sag last saw activity: a new mail, or anyone changing its column, assignee or tags. What the
    # time rules (MailTimeRule) go by; NULL only for a row from before it existed (the migration fills it).
    last_activity_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_message_at", "-pk"]

    def __str__(self) -> str:
        return self.subject or "(intet emne)"


class MailMessage(models.Model):
    """One message's headers, as last seen on the server. Never the body — see the module docstring."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="messages")
    folder = models.ForeignKey(MailFolder, on_delete=models.CASCADE, related_name="messages")
    # SET_NULL: a sag with no mails left is deleted (or, while a folder is unreadable, stamped `emptied_at`)
    # at the end of a sync, never by cascade.
    thread = models.ForeignKey(
        MailThread, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    uid = models.PositiveBigIntegerField()
    message_id = models.CharField(max_length=998, blank=True)
    in_reply_to = models.TextField(blank=True)
    references = models.TextField(blank=True)
    subject = models.CharField(max_length=998, blank=True)
    from_name = models.CharField(max_length=255, blank=True)
    from_address = models.CharField(max_length=320, blank=True)
    to = models.TextField(blank=True)
    cc = models.TextField(blank=True)
    date = models.DateTimeField(null=True, blank=True)
    size = models.PositiveIntegerField(default=0)
    seen = models.BooleanField(default=False)
    answered = models.BooleanField(default=False)
    has_attachments = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date", "-uid"]
        constraints = [models.UniqueConstraint(fields=["folder", "uid"], name="postkasser_unique_uid")]
        # Threading looks mails up by Message-ID for every new mail of a sync.
        indexes = [models.Index(fields=["mailbox", "message_id"], name="postkasser_msgid")]

    def __str__(self) -> str:
        return self.subject or "(intet emne)"

    @property
    def sender(self) -> str:
        return self.from_name or self.from_address


class MailRule(models.Model):
    """A rule for incoming mail, kept per mailbox and edited by anyone who can open it: when a new mail
    matches ALL of the conditions that are filled in, its sag gets the actions. Run in position order
    (services.sync, rules.py); `stop_processing` ends the run for that mail.

    A folder that disappears from the server switches the rules that named it OFF first
    (services._refresh_folders, with `disabled_reason`) and then the link is cleared (SET_NULL): turning
    "mail in Faktura" into "all mail" would be the worse failure, so a rule with no condition left never
    matches (rules.matches). A deleted column or resident just drops that action."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField("Navn", max_length=60)
    position = models.PositiveIntegerField("Placering", default=0)
    is_active = models.BooleanField("Aktiv", default=True)
    stop_processing = models.BooleanField("Stop efter denne regel", default=False)

    from_contains = models.CharField("Afsender indeholder", max_length=200, blank=True)
    subject_contains = models.CharField("Emne indeholder", max_length=200, blank=True)
    to_contains = models.CharField("Til/Cc indeholder", max_length=200, blank=True)
    folder = models.ForeignKey(
        MailFolder, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="Mappe"
    )
    # Why the system (not a person) switched the rule off, shown on the Automatisering page; cleared when
    # the rule is saved again.
    disabled_reason = models.CharField(max_length=255, blank=True, editable=False)

    add_tags = models.ManyToManyField(MailTag, blank=True, related_name="+", verbose_name="Tilføj tags")
    set_column = models.ForeignKey(
        BoardColumn,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Flyt til kolonne",
    )
    set_assignee = models.ForeignKey(
        Resident, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="Ansvarlig"
    )

    class Meta:
        ordering = ["position", "pk"]
        verbose_name = "regel"
        verbose_name_plural = "regler"

    def __str__(self) -> str:
        return self.name


class MailTimeRule(models.Model):
    """ "Sager in <from_column> with no activity for <after_days> days move to <to_column>" — applied by the
    hourly job (rules.apply_time_rules). Both columns belong to the mailbox and differ."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="time_rules")
    name = models.CharField("Navn", max_length=60)
    is_active = models.BooleanField("Aktiv", default=True)
    # SET_NULL: a deleted column switches the rule off (columns.deactivate_time_rules) and is kept, with
    # the reason, instead of silently vanishing; a rule with a missing column does nothing.
    from_column = models.ForeignKey(
        BoardColumn, null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="Sager i kolonnen"
    )
    after_days = models.PositiveSmallIntegerField(
        "Dage uden aktivitet", validators=[MinValueValidator(1), MaxValueValidator(365)]
    )
    to_column = models.ForeignKey(
        BoardColumn, null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="Flyttes til"
    )
    disabled_reason = models.CharField(max_length=255, blank=True, editable=False)

    class Meta:
        ordering = ["pk"]
        verbose_name = "tidsregel"
        verbose_name_plural = "tidsregler"

    def __str__(self) -> str:
        return self.name


# A draft nobody has touched for this long is purged by the daily job (postkasser.tasks.purge_drafts).
DRAFT_TTL = timedelta(days=30)
# Server-side limits on a draft. Over them the save is REFUSED with a message, never truncated: cutting
# somebody's text off silently is worse than saying no.
DRAFT_MAX_BODY = 100_000
DRAFT_MAX_SUBJECT = 998
DRAFT_MAX_ADDRESSES = 10_000  # `to` / `cc`, raw text


class MailDraft(models.Model):
    """A mail somebody has started but not sent. Stored here and nowhere else (see the module docstring).

    Every field is the RAW text of the compose form: addresses are NOT validated when saving, because a
    half-typed address must save too — they are checked only on send (forms.ComposeForm).

    `thread` is None for a new mail and the sag for a reply; at most one draft per author and sag (the
    constraint below), so an autosave that does not yet know its draft's pk finds the same row again.
    A new mail has no sag, so one author may have any number of those.

    Visibility: anyone who can open the mailbox sees that a draft exists on a sag and can read it; only the
    author can edit, continue, send or delete it. If the author later loses access to the mailbox, the
    draft stays visible on the sag to the others (so they know half an answer exists) until the purge
    removes it; nobody else can delete it."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="drafts")
    author = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="+")
    thread = models.ForeignKey(
        MailThread, null=True, blank=True, on_delete=models.CASCADE, related_name="drafts"
    )
    # The mail being answered. SET_NULL: the cached header may go; the draft (and its sag) stays.
    reply_to = models.ForeignKey(
        MailMessage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reply_all = models.BooleanField(default=False)
    to = models.TextField(blank=True)
    cc = models.TextField(blank=True)
    subject = models.CharField(max_length=DRAFT_MAX_SUBJECT, blank=True)
    body = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["author", "thread"],
                condition=models.Q(thread__isnull=False),
                name="postkasser_one_draft_per_author_thread",
            )
        ]
        verbose_name = "kladde"
        verbose_name_plural = "kladder"

    def __str__(self) -> str:
        return self.subject or "(intet emne)"


def shown(prefix: str = "") -> models.Q:
    """Sager the board shows: not stamped empty by a sync (MailThread.emptied_at), and either holding a
    mail or waiting for the copy of one sent from the intranet. `prefix` is the lookup path to the sag
    ("threads__" from a column or a tag)."""
    has_mail = MailMessage.objects.exclude(thread=None).values("thread_id")
    return models.Q(**{f"{prefix}emptied_at__isnull": True}) & (
        models.Q(**{f"{prefix}pending_message_id__gt": ""}) | models.Q(**{f"{prefix}pk__in": has_mail})
    )


class MailEvent(models.Model):
    class Action(models.TextChoices):
        READ = "read", "Læst"
        REPLY = "reply", "Besvaret"
        SEND = "send", "Sendt"
        STATUS = "status", "Flyttet til"
        ASSIGN = "assign", "Tildelt"
        MOVE = "move", "Flyttet til mappe"
        TAG = "tag", "Tags"
        UNREAD = "unread", "Markeret som ulæst"
        RULE = "rule", "Regel"

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="events")
    # SET_NULL: the log outlives the cached header, which goes when the mail is deleted at one.com.
    message = models.ForeignKey(
        MailMessage, null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )
    thread = models.ForeignKey(
        MailThread, null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )
    resident = models.ForeignKey(Resident, null=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    subject = models.CharField(max_length=998, blank=True)
    # The human-readable target of a STATUS / ASSIGN / MOVE / TAG: the column's name, the assignee's name,
    # the folder's name, the sag's tags.
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_action_display()}: {self.subject}"
