# Feature: GAHKFlix (video service)

GAHKFlix is the kollegium's own streaming service for films residents have made — revyer, sketches,
fester. It is a dark, full-page area in the style of Netflix at `/gahkflix/`, without the
intern site's sidebar and top bar; its own header links back to the intern site.

## Access

- While it is under test, only administrators and Fotogruppen can open GAHKFlix. The sidebar entry,
  every page, its notifications and the profile strip follow the same gate, and the header shows
  `Under test`. Opening it to every resident is one setting. In local development (DEBUG) every
  resident is let in, so the view of a non-administrator can be tried.
- In GAHKFlix, "administrators" means the administrator role and Fotogruppen.

## Browsing

- The home page leads with a banner for one of the most watched (else newest) videos, a different one
  each day, then rows: `Fortsæt med at se`, `Kommer snart` (upcoming premieres, soonest first), `Top
  10 på GAHKFlix lige nu`, `Nyeste` (by when each came out: its premiere, else its publishing), one row
  for each of the most used tags (up to four, each with at least two videos) and `Serier`.
- Top 10 ranks videos by unique viewers over the last 7 days.
- A card shows the thumbnail, title, series and episode, year and length, and a red bar for how far
  the reader has watched.
- A video's page has its banner, release date (as much of it as is known: `12. marts 2015`, `marts
  2015` or `2015`), length, quality, views and unique viewers, description, credits (each linking to
  that person's cast page), tags, the series' episodes by season, `Mere som denne` (videos sharing
  tags or cast) and `Hent`: HD, Full HD (when there is one) or the original, each with its size.
- A published video's page has `Del på opslagstavlen`, which opens a new post with the video
  embedded. The post shows it as a small GAHKFlix hero (see opslagstavle.md).
- Series pages list the published episodes by season and episode. The timeline lists every video by
  its release date (else its upload), grouped by year and month, with a jump to each year; a video
  known only by its year comes after that year's months, under `Uden måned`.
- Search matches titles, descriptions, tags, credited names and roles, and series, every word
  somewhere; results update as you type. Tag pages and cast pages (one per resident, one per
  external name) list their videos.
- A resident's profile shows `Medvirker i` with the videos they are credited in.

## Playing

- A 4-second GAHKFlix intro plays before every film and can be skipped. `Afspil` opens the player
  over the page and starts the intro at once; the address becomes the player's, and Back closes it.
  A link straight to the player may need a click on its play button first.
- Every video has an HD rendition and, when the original is larger than HD, a Full HD one. Nothing is
  upscaled, cropped or stretched: a video keeps its own shape (portrait, 4:3, 15:9, …) and the player
  shows it whole, with black bars where it does not fill the screen. The player switches quality without losing its place,
  remembers the choice in the browser, and otherwise starts in the best quality there is.
- The player has its own controls, the same in every browser. At the bottom: a timeline (what has
  loaded, the moment under the pointer, click or drag to go there), play/pause, 10 seconds back and
  forward, sound (mute, and a volume slider where there is a mouse), time played of the whole,
  subtitles, a settings menu with quality and speed (0.5× to 2×), picture-in-picture where the
  browser has it, and fullscreen. At the top: back (the arrow and the title together), and the
  autoplay switch. Both fade out after 3 seconds of playing undisturbed.
- A click on the picture plays or pauses; a double click is fullscreen. With a mouse, a big play
  button sits in the middle while the film is paused, and starting it again shows for a moment
  there. With a finger, a tap shows or hides the controls, which have play/pause in the middle too,
  and a double tap on the left or right third jumps 10 seconds back or forward. Keys: Space or K play/pause, ← → or J L jump 10 seconds,
  ↑ ↓ volume, F fullscreen, M mute, C subtitles on/off, Escape closes a menu, then the end card,
  then the player.
- The installed app plays fullscreen, from the first click or tap the browser allows it on; once
  left, it stays as left. Fullscreen on a phone or tablet turns the screen to the film's shape
  (landscape for a wide film), so it fills the screen without turning the phone; the rest of the
  app stays upright.
- Sound (volume and mute) is remembered in the browser; speed holds while the player is open (the
  intro always plays at normal speed).
- A video keeps one sound track: the one with the most channels, as ffmpeg would choose. A track
  with nothing in it (an old camera's broken one) is never taken, and a video with no usable sound
  is kept silent rather than refused.
- Text subtitles (SRT, ASS, the MP4 kind) are kept as WebVTT files beside the renditions, each with
  its language and its default and forced flags. Picture subtitles (DVD, Blu-ray) cannot become text,
  and only the original keeps them.
- The subtitles menu names each track by its language in Danish (`Dansk`, `Engelsk (Kommentarer)`),
  with `Fra` for none. The choice is remembered by language: a film with that language starts with
  it, any other with the track the file marks as its default, if any. Forced tracks (signs, foreign
  speech) are not in the menu; they show whenever nothing else is chosen. Subtitles are drawn above
  the controls while those show, and lower when they fade.
- Playback resumes where the reader stopped, once they are 10 seconds in. Within the last 5% (at
  least 5 seconds) a video counts as watched and starts from the beginning next time.
- A view counts after 5 seconds of actual playback, or half of a very short video. Views by the same
  resident within 10 minutes count once.
- Every time the player opens a published video is a viewing, a view or not. Opening it again within
  10 minutes of leaving it short of the end goes on with the same viewing. The player reports how
  much of the film actually played (skipping adds nothing) and whether it reached the end.
- The uploader and administrators see statistics on the video's page: how often it was opened in the
  player, views, how many views reached the end, the average time a view played (and its share of
  the film), unique viewers, views in the last 7 days and views per day for the last 30 days. Playing
  time is known only for views since the player began reporting it; shares are of those views.
- Autoplay: when a video ends, an end card shows what is next and starts it by itself after 8
  seconds (`Afspil nu` starts it at once, `Annuller` or Escape stays). In a series that is the next
  episode, and `Næste afsnit` also appears in the last 20 seconds. After a film, or the last
  episode, it is the most similar film the resident hasn't finished, else the most trending, else
  the newest. Episodes of other series are not picked. Once every film is finished, it is the film
  watched longest ago.
- Autoplay can be switched off in the player, remembered per browser; the end card then waits. After
  3 autoplays in a row with no key press or click, it asks `Ser du stadig med?` instead.
- Fullscreen is always the whole player, so the end card shows there too. On an iPhone, where
  fullscreen is the system's own player (with the chosen subtitles), the video leaves fullscreen
  when it ends if there is something next.

## Uploading

- Any resident with access may upload: MP4, MOV, M4V, WebM, AVI, MKV, 3GP, MTS, MPEG, WMV or OGV, up
  to 5 GB. The file goes straight from the browser to object storage, with a progress bar.
- A resident may have up to 10 unpublished drafts, those still being processed included, and may
  upload while earlier uploads wait or are processed. Several files can be picked or dropped at once;
  they are sent one after another, each with its own progress and a link to its edit page. More files
  than there is room for are refused rather than the first few taken; with 10 drafts the upload page
  says to publish or delete one first. A new version waits until the version before it is processed.
  The upload page first lists the uploader's recent videos — unpublished ones (processing, failed or
  waiting to be finished) and those uploaded in the last week, each with its state — so one already
  uploaded is not uploaded again.
- While an upload waits for the encodes before it, its edit page says it is in the queue and how many
  go first, and the lists say `I kø`, rather than showing 0 %. It starts by itself.
- The same file cannot be uploaded twice, by anyone. Before anything is sent, a fingerprint of the
  file is compared with every video's; once it has arrived, its full checksum is, binned videos
  included. A duplicate is refused, naming the video it duplicates.
- An upload is a draft until it is published. While it is processed, the uploader can fill in the
  title, description, release date, series, season and episode, credits (residents, or people from
  outside by name, each with an optional role) and tags (the photo album's tag rules).
- The release date (`Udgivelsesdato`) is known to the day, the month or only the year, and must be
  filled in by hand, at least the year, to publish. Once a video has one it cannot be emptied; an
  older video without one can still be edited without it. It cannot lie in the future, unless the
  video is a premiere (below).
- The date the file suggests is guessed when it is processed and only offered: `Brug videoens dato`
  puts it in the fields, with where it came from beside it. The guess: a whole date written in the
  file's name or in its date tags first; else the camera's clock, unless a year (or month) written
  there disagrees; else that year or month; else the file's last-modified time. A new version of
  the file brings its own guess and never changes the date.
- The edit page links to the original file (`Hent originalen`).
- The thumbnail and the banner are picked from 10 random frames spread over the video, with black and
  flat frames redrawn; `Vis 10 nye billeder` draws another ten, which replace the ones shown without
  the page reloading. Or the uploader picks the moment themselves in a player, a second or a frame at
  a time, and it is cut from the best rendition. Either may also be a picture of the uploader's own.
  The edit page shows what is chosen now apart from the options, under a ruler.
- What is typed on the edit page and not yet saved outlives whatever reloads the page — picking a
  picture, a new version, the processing finishing — with a note that it is not saved; `Fortryd det`
  drops it. `Udgiv videoen` saves what is typed before it publishes.
- Thumbnails and banners are 16:9. A picture close to that shape (from 4:3 to cinema widescreen) is
  cropped to it; any other — a phone held upright, a square, a panorama — is shown whole on a
  blurred, darkened copy of itself, so nothing important is cut off. A draft can only be published with a title, a finished file,
  a thumbnail and a banner.
- Publishing notifies everyone with access and the credited residents (an upcoming premiere: at its
  premiere instead). The uploader is notified
  when processing has finished or failed. Only the notifications about new videos and about being
  credited can be turned off; those about one's own uploads and requests, approvals and drafts always
  appear in the app, and only their push can be switched off.
- Only the uploader and administrators see a draft. A draft never published is deleted with its files
  90 days after the day it was uploaded (or made a draft again); the edit page and `Mine videoer` show the date.
- Every Monday, each uploader is reminded of their drafts more than a week old, and the administrators
  get an overview of all of them. The day before a draft is deleted, its uploader and the
  administrators are warned. Administrators see every draft under `Kladder`.

## Premieres

- Beside `Udgivelsesdato` is a `Premiere` box. Ticked, the release is a whole moment — year, month,
  day and a time to the minute, all required — and may lie up to a year ahead. Only a premiere has a
  time. A video out for more than a day cannot be given a premiere in the future. Within the day it
  can, so one published by mistake can still be made a premiere.
- A premiere already past only records when the film was first shown. On a video already published,
  it must lie before the publishing: a premiere after it would have to be in the future.
- A premiere chooses how its thumbnail is shown until then: `Ikke sløret`, `Lidt sløret`, `Middel
  sløret` or `Meget sløret`. The edit page previews it.
- Published before its premiere, a video is upcoming: it is listed in `Kommer snart`, its series and
  the timeline as a card showing only its title, series, season and episode, the exact premiere
  (`24. december 2026 kl. 20.00`) and its thumbnail as chosen. Its page, player, files and banner
  stay closed to everybody but its uploader and the administrators, for whom the card is a link;
  it is not found by search, tags, cast pages, `Mere som denne`, Top 10 or autoplay.
- At its premiere it opens to everybody, and everybody with access and its credited residents are
  notified, as at publishing (`Premiere på GAHKFlix: …`); the history notes it. A premiere already
  past when the video is published is out at once and announced as any new video (`Ny på GAHKFlix:
  …`). Taking an upcoming premiere away brings the video out, and announces it, at once. Once out,
  its page shows the date only, not the time.
- Every day at 10.20, an uploader with drafts whose premiere is still ahead is warned they are not
  published (urgent within the last day): unpublished, there is no premiere.
- Until its premiere, a video is open as a draft is: edited freely, given a new version or binned by
  its uploader. Afterwards the time windows (7 days for new versions and binning, 30 for free edits)
  run from the premiere, or from the upload when the premiere was already past at publishing.

## Taking a video back

- For a day after a video comes out (its premiere, else its publishing), its uploader or an
  administrator may make it a draft again (`Gør til kladde igen`), or give it a premiere later. An
  upcoming premiere may be made a draft again at any time.
- Either way, the notifications that it was out, and to its cast, are taken back (a push already shown
  cannot be), and it is announced again when it comes out. Views counted meanwhile stay.
- A draft again counts its 90 days, and its reminders, from then rather than from the upload. It may
  take its uploader past 10 drafts; they then cannot upload until they are below it again.

## Series

- Any resident may create a series and put their own videos in it. Titles are unique, ignoring case,
  and so is an episode number within a series' season. `Opret en ny serie` on the edit page creates
  one in a dialog and picks it for the video, to be saved with the rest.
- Season and episode take digits only. Each has a dropdown of what the series already has — its
  seasons with their number of episodes, the chosen season's episodes with their titles — and offers
  the free numbers: the next one, and any gap. Taken numbers are shown but cannot be picked; an
  earlier free number can, even when later ones exist.
- A series' pictures are those of its first episode out; with only upcoming premieres, the first one's
  thumbnail as it is shown before its premiere, and no banner.
- Its creator and administrators may edit it, under the same 30-day rule as videos. It can only be
  deleted when it holds no videos at all, drafts and binned ones included.

## Editing and new versions

- The uploader and administrators may edit a video's details and pictures: a draft at any time, a
  published video freely for 30 days after the upload. After that, a change is a proposal that one
  administrator other than the proposer must approve. A proposer's further changes join their open
  proposal; while somebody else's is open, a new one has to wait.
- A new version of the file may be uploaded by the uploader or an administrator within 7 days of the
  upload (a draft at any time). The current version keeps playing until the new one is ready; the
  details, pictures and views stay, and new frames are offered.
- It is the way to correct a video — a wrong cut, bad sound, a missing scene — and the edit page says
  so where the new version is uploaded, with its deadline. Once the 7 days are over, it says why
  there is no new version.

## History

- Every video has a `Historik`, for its uploader and the administrators: who did what and when —
  uploads and new versions, processing finished or failed, every change
  to the details and the pictures with before and after, publishing, premieres, being made a draft
  again, binning and restoring. An
  approved proposal is the proposer's change, marked as approved. A video uploaded before there was
  a history has only what happened since.
- Every thumbnail and banner a video has had is kept as long as the video is. The edit page offers
  the latest earlier ones to bring back, and the history all of them; after 30 days that is a
  proposal, like any change.

## Deleting and the bin

- A draft is deleted outright, by its uploader or an administrator.
- Within 7 days of the upload, the uploader or an administrator moves a published video to the bin.
- After 7 days only an administrator may ask, with a reason, and two other administrators must
  approve. One rejection closes the request.
- Administrators are notified of every request; the one who asked is told the outcome and may
  withdraw it until then. A request nobody decides expires after 30 days. Binning a video closes the
  open proposals about it.
- Administrators see the bin and can restore from it. A binned video is deleted with all its files 30
  days after it was binned.
- A stored file no video, frame, open proposal or history refers to — an upload abandoned before it
  was saved, or what a crashed encode left — is deleted once it is two days old. If that would be more than half
  of everything stored, nothing is deleted and the error is logged: the database does not match the
  storage.

## Decisions

- GAHKFlix is separate from the photo album, which keeps the videos it already holds.
- Uploads bypass the app server: a multi-gigabyte film over dorm wifi cannot hold a web worker.
- Two MP4 renditions and a quality switch rather than adaptive streaming: no extra player to ship,
  and nothing to break on a phone.
- The time windows run from the upload, but a draft is its uploader's own until published. A premiere
  still ahead at publishing counts as the upload: the windows guard what people have seen, and nobody
  sees a premiere before its minute (it could be made a draft and changed anyway). A premiere set
  after the publishing yet already past is refused rather than counted: it would open the windows
  again, and put the video first under the newest, as often as anybody liked. Its
  deletion is counted in calendar days, so it has one date that every reminder can name.
- Reminders go out in the morning, not with the nightly sweeps: they are read by people.
- The player opens over the page rather than as a page of its own because a browser only lets a page
  play sound from inside the click that asked for it, and a page load loses that click.
- Duplicates are checked across all videos, not only the uploader's: one film should exist once.
- Encodes run one at a time on the media worker. One that stops reporting, or whose ffmpeg is stopped
  (most often for want of memory), is tried again, at most three times in all, and then marked failed
  for the uploader to see; that says nothing about the file. A file ffmpeg refuses is marked failed at
  once. Either way the job is listed as failed on the worker-jobs page with the reason, and a job
  Celery sends again after its worker died says so instead of passing as done.
- Subtitles are kept as files of their own rather than inside the MP4s: WebVTT is what a browser's
  player reads, and inside an MP4 only Safari shows them. They are served from the app, not
  redirected to the bucket: a browser loads a subtitle track only from the page's own origin, unless
  the bucket sends CORS headers.
- The controls are built by hand rather than taken from a player library: the player already runs its
  own flow (intro, one video element for intro, film and next episode, the end card), which a library
  would want to own. The browser's own controls looked different everywhere and sat under ours.
- The file's date is offered, not filled in: a guess filled in by itself was saved and published
  unchecked, and an empty date is a question the uploader must answer.
- A date somebody wrote beats the camera's clock in the guess: an exported or re-encoded film carries
  the day it was exported, while `Revy 2009.mp4` says what it is. A camera's file counter
  (`IMG_1999`) is not taken for a year.
- A premiere's thumbnail is blurred on the server: blurred in the browser, the sharp picture would be
  sent along. Its address names the blur, so a browser never keeps the blurred one past the premiere.
- A premiere's page opens at its minute by the clock, and its announcement is a job lined up for that
  minute — but only once it is within 20 minutes: published at shorter notice it is lined up at once,
  otherwise by a sweep every quarter hour, which also announces anything that came out unannounced.
  Not a job every minute (1440 a day on the worker-jobs page), and not one waiting days for its
  minute, which the database broker can lose to a worker restart or its nightly sweep.
- Unsaved details are kept in the browser tab rather than saved as they are typed: after 30 days a
  saved change is a proposal to approve, which must stay something done on purpose.
- Bringing a picture back stores a copy of it: a proposal that is turned down deletes its pictures,
  and must not take the history's with it.
