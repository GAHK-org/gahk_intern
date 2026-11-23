"""What the audit trail keeps of GAHKFlix.

GAHKFlix keeps a history of its own as well (VideoEvent): the pictures a video has had, so they can
be brought back and their files kept, deleted with the video and its files. That is the feature's
bookkeeping; this trail is the record, and outlives the video.
"""

from audit import trail

from .models import (
    CastMember,
    ChangeRequest,
    ChangeVote,
    Series,
    Tag,
    Video,
    VideoEvent,
    VideoFile,
    VideoFrame,
    VideoSubtitle,
    ViewEvent,
    Watch,
)

trail.register(
    Video,
    "gahkflix.video",
    name=lambda video: video.title,
    label="Video",
    # Its fixed id, its upload time, the reminder bookkeeping of drafts, and when everybody was told
    # it is out and since when it is a draft again: the publish, premiere and unpublish events say so.
    ignore={"uuid", "created_at", "draft_warned_at", "announced_at", "draft_since"},
    labels={
        "title": "Titel",
        "description": "Beskrivelse",
        "uploaded_by": "Uploadet af",
        "published_at": "Udgivet",
        "released_on": "Udgivelsesdato",
        "premiere_at": "Premiere",
        "premiere_blur": "Miniature før premieren",
        "release_precision": "Datoens præcision",
        "release_source": "Datoens kilde",
        "series": "Serie",
        "season": "Sæson",
        "episode": "Afsnit",
        "tags": "Tags",
        "file": "Fil",
        "thumbnail": "Miniature",
        "banner": "Banner",
        "thumbnail_frame": "Miniature fra billede",
        "banner_frame": "Banner fra billede",
        "deleted_at": "Lagt i papirkurven",
        "deleted_by": "Lagt i papirkurven af",
    },
)
trail.register(
    VideoFile,
    "gahkflix.video_file",
    name=lambda video_file: video_file.original_name,
    label="Videofil",
    # What the worker finds out about the file and makes from it, and how far it has got.
    ignore={
        "uuid",
        "created_at",
        "content_type",
        "fingerprint",
        "file_modified_at",
        "progress",
        "attempts",
        "task_id",
        "heartbeat_at",
        "completed_at",
        "duration",
        "width",
        "height",
        "recorded_at",
        "date_guess",
        "date_guess_source",
        "full_hd",
        "hd",
        "full_hd_size",
        "hd_size",
    },
    labels={
        "video": "Video",
        "uploaded_by": "Uploadet af",
        "original": "Original",
        "original_name": "Filnavn",
        "size": "Størrelse",
        "state": "Tilstand",
        "error": "Fejl",
    },
)
trail.register(
    Series, "gahkflix.series", name=lambda series: series.title, label="Serie", ignore={"created_at"}
)
trail.register(Tag, "gahkflix.tag", name=lambda tag: tag.name, label="Tag")
trail.register(
    CastMember,
    "gahkflix.cast_member",
    name=lambda member: member.display_name,
    label="Medvirkende",
    labels={
        "video": "Video",
        "resident": "Beboer",
        "name": "Navn",
        "role": "Rolle",
        "position": "Rækkefølge",
    },
)
trail.register(
    ChangeRequest,
    "gahkflix.change_request",
    name=lambda change: f"{change.get_kind_display()}: {change.video or change.series}",
    label="Anmodning",
    ignore={"created_at"},
    labels={
        "kind": "Slags",
        "video": "Video",
        "series": "Serie",
        "requested_by": "Anmodet af",
        "changes": "Ændringer",
        "reason": "Begrundelse",
        "state": "Tilstand",
        "decided_at": "Afgjort",
        "note": "Note",
    },
)
trail.register(
    ChangeVote,
    "gahkflix.change_vote",
    name=lambda vote: f"{vote.voter.full_name}: {'ja' if vote.approve else 'nej'}",
    label="Stemme",
    ignore={"created_at"},
    labels={"request": "Anmodning", "voter": "Stemt af", "approve": "Godkendt", "comment": "Kommentar"},
)
trail.exempt(VideoFrame, "stills the worker draws from the video; choosing one is recorded on the video")
trail.exempt(VideoSubtitle, "subtitle tracks the worker takes from the uploaded file")
trail.exempt(Watch, "how far a resident has watched: viewing, not a change")
trail.exempt(ViewEvent, "a view counted for the statistics: viewing, not a change")
trail.exempt(VideoEvent, "the video's own history and picture bookkeeping; each line's event is in the trail")

trail.actions(
    {
        "gahkflix.video.uploaded": "Uploadede videoen",
        "gahkflix.video.new_version": "Uploadede en ny version af videoen",
        "gahkflix.video.processed": "Færdigbehandlede videoen",
        "gahkflix.video.failed": "Kunne ikke behandle videoen",
        "gahkflix.video.edited": "Rettede videoen",
        "gahkflix.video.published": "Udgav videoen",
        "gahkflix.video.unpublished": "Gjorde videoen til en kladde igen",
        "gahkflix.video.premiered": "Videoen havde premiere",
        "gahkflix.video.announced": "Videoen blev annonceret",
        "gahkflix.video.binned": "Lagde videoen i papirkurven",
        "gahkflix.video.restored": "Hentede videoen tilbage fra papirkurven",
        "gahkflix.video.deleted": "Slettede videoen for altid",
        "gahkflix.series.created": "Oprettede serien",
        "gahkflix.series.edited": "Rettede serien",
        "gahkflix.series.deleted": "Slettede serien",
        "gahkflix.change.proposed": "Foreslog en ændring",
        "gahkflix.change.deletion_requested": "Bad om at slette videoen",
        "gahkflix.change.voted": "Stemte om en ændring",
        "gahkflix.change.approved": "Godkendte en ændring",
        "gahkflix.change.rejected": "Afviste en ændring",
        "gahkflix.change.withdrawn": "Trak en ændring tilbage",
        "gahkflix.change.expired": "Ændringen udløb",
    }
)
