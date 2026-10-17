# Feature: GAHKFlix (video service)

GAHKFlix is the kollegium's own streaming service for films residents have made — revyer, sketches,
fester. It is a dark, full-page area in the style of Netflix at `/intern/gahkflix/`, without the
intern site's sidebar and top bar; its own header links back to the intern site.

## Access

- While it is under test, only administrators and Fotogruppen can open GAHKFlix. The sidebar entry,
  every page, its notifications and the profile strip follow the same gate, and the header shows
  `Under test`. Opening it to every resident is one setting. In local development (DEBUG) every
  resident is let in, so the view of a non-administrator can be tried.
- In GAHKFlix, "administrators" means the administrator role and Fotogruppen.

## Browsing

- The home page leads with a banner for one of the most watched (else newest) videos, a different one
  each day, then rows: `Fortsæt med at se`, `Top 10 på GAHKFlix lige nu`, `Nyeste`, one row for each
  of the most used tags (up to four, each with at least two videos) and `Serier`.
- Top 10 ranks videos by unique viewers over the last 7 days.
- A card shows the thumbnail, title, series and episode, year and length, and a red bar for how far
  the reader has watched.
- A video's page has its banner, date, length, quality, views and unique viewers, description,
  credits (each linking to that person's cast page), tags, the series' episodes by season, `Mere som
  denne` (videos sharing tags or cast) and `Hent`: HD, Full HD (when there is one) or the original,
  each with its size.
- Series pages list the published episodes by season and episode. The timeline lists every video by
  its date, grouped by year and month, with a jump to each year.
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
  remembers the choice in the browser, and starts phones in HD.
- Playback resumes where the reader stopped, once they are 10 seconds in. Within the last 5% (at
  least 5 seconds) a video counts as watched and starts from the beginning next time.
- A view counts after 5 seconds of actual playback, or half of a very short video. Views by the same
  resident within 10 minutes count once.
- The uploader and administrators see statistics on the video's page: views, unique viewers, views
  in the last 7 days and views per day for the last 30 days.
- In a series, `Næste afsnit` appears in the last 20 seconds and starts by itself 8 seconds after the end.

## Uploading

- Any resident with access may upload: MP4, MOV, M4V, WebM, AVI, MKV, 3GP, MTS, MPEG, WMV or OGV, up
  to 5 GB. The file goes straight from the browser to object storage, with a progress bar.
- Nobody can start an upload while one of theirs is still being processed. The upload page lists
  the uploader's unpublished videos first, so one already on its way is finished rather than
  uploaded again.
- The same file cannot be uploaded twice, by anyone. Before anything is sent, a fingerprint of the
  file is compared with every video's; once it has arrived, its full checksum is, binned videos
  included. A duplicate is refused, naming the video it duplicates.
- An upload is a draft until it is published. While it is processed, the uploader can fill in the
  title, description, date, series, season and episode, credits (residents, or people from outside
  by name, each with an optional role) and tags (the photo album's tag rules).
- The date is read from the video's own metadata, else the file's last-modified time. It can be set
  by hand, and then stays as set.
- The thumbnail and the banner are picked from 10 random frames spread over the video, with black and
  flat frames redrawn; `Vis 10 nye billeder` draws another ten. Or the uploader picks the moment
  themselves in a player, a second or a frame at a time, and it is cut from the best rendition. The
  thumbnail may also be the uploader's own picture. The edit page shows what is chosen now apart
  from the options, under a ruler.
- Thumbnails and banners are 16:9. A picture close to that shape (from 4:3 to cinema widescreen) is
  cropped to it; any other — a phone held upright, a square, a panorama — is shown whole on a
  blurred, darkened copy of itself, so nothing important is cut off. A draft can only be published with a title, a finished file,
  a thumbnail and a banner.
- Publishing notifies everyone with access and the credited residents. The uploader is notified
  when processing has finished or failed. Only the notifications about new videos and about being
  credited can be turned off; those about one's own uploads and requests, approvals and drafts cannot.
- Only the uploader and administrators see a draft. A draft never published is deleted with its files
  90 days after the day it was uploaded; the edit page and `Mine videoer` show the date.
- Every Monday, each uploader is reminded of their drafts more than a week old, and the administrators
  get an overview of all of them. The day before a draft is deleted, its uploader and the
  administrators are warned. Administrators see every draft under `Kladder`.

## Series

- Any resident may create a series and put their own videos in it. Titles are unique, ignoring case,
  and so is an episode number within a series' season.
- Season and episode take digits only. Each has a dropdown of what the series already has — its
  seasons with their number of episodes, the chosen season's episodes with their titles — and offers
  the free numbers: the next one, and any gap. Taken numbers are shown but cannot be picked; an
  earlier free number can, even when later ones exist.
- A series' pictures are those of its first episode.
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
- It is the way to correct a video — a wrong cut, bad sound, a missing scene — and the site says so:
  the upload page opens with it and lists the uploader's videos that can still get a new version,
  each with its deadline, and the edit page points to it. Once the 7 days are over, the edit page
  says why there is no new version.

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
- A stored file no video, frame or open proposal refers to — an upload abandoned before it was saved,
  or what a crashed encode left — is deleted once it is two days old. If that would be more than half
  of everything stored, nothing is deleted and the error is logged: the database does not match the
  storage.

## Decisions

- GAHKFlix is separate from the photo album, which keeps the videos it already holds.
- Uploads bypass the app server: a multi-gigabyte film over dorm wifi cannot hold a web worker.
- Two MP4 renditions and a quality switch rather than adaptive streaming: no extra player to ship,
  and nothing to break on a phone.
- The time windows run from the upload, but a draft is its uploader's own until published. Its
  deletion is counted in calendar days, so it has one date that every reminder can name.
- Reminders go out in the morning, not with the nightly sweeps: they are read by people.
- The player opens over the page rather than as a page of its own because a browser only lets a page
  play sound from inside the click that asked for it, and a page load loses that click.
- Duplicates are checked across all videos, not only the uploader's: one film should exist once.
- Encodes run one at a time on the media worker. One that stops reporting is retried, at most three
  times, and then marked failed for the uploader to see.
