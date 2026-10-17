# Feature: GAHKFlix (video service)

GAHKFlix is the kollegium's own streaming service for films residents have made — revyer, sketches,
fester. It is a dark, full-page area in the style of Netflix at `/intern/gahkflix/`, without the
intern site's sidebar and top bar; its own header links back to the intern site.

## Access

- While it is under test, only administrators and Fotogruppen can open GAHKFlix. The sidebar entry,
  every page, its notifications and the profile strip follow the same gate, and the header shows
  `Under test`. Opening it to every resident is one setting.
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
  denne` (videos sharing tags or cast) and `Hent original`.
- Series pages list the published episodes by season and episode. The timeline lists every video by
  its date, grouped by year and month, with a jump to each year.
- Search matches titles, descriptions, tags, credited names and roles, and series, every word
  somewhere; results update as you type. Tag pages and cast pages (one per resident, one per
  external name) list their videos.
- A resident's profile shows `Medvirker i` with the videos they are credited in.

## Playing

- A 4-second GAHKFlix intro plays before every film and can be skipped.
- Every video has an HD rendition and, when the original is larger than HD, a Full HD one. Nothing is
  upscaled, and portrait video stays portrait. The player switches quality without losing its place,
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
- Nobody can start an upload while one of theirs is still being processed.
- The same file cannot be uploaded twice, by anyone. Before anything is sent, a fingerprint of the
  file is compared with every video's; once it has arrived, its full checksum is, binned videos
  included. A duplicate is refused, naming the video it duplicates.
- An upload is a draft until it is published. While it is processed, the uploader can fill in the
  title, description, date, series, season and episode, credits (residents, or people from outside
  by name, each with an optional role) and tags (the photo album's tag rules).
- The date is read from the video's own metadata, else the file's last-modified time. It can be set
  by hand, and then stays as set.
- The thumbnail and the banner are picked from 10 random frames spread over the video, with black and
  flat frames redrawn; `Vis 10 nye billeder` draws another ten. The thumbnail may instead be the
  uploader's own picture, cut to 16:9. A draft can only be published with a title, a finished file,
  a thumbnail and a banner.
- Publishing notifies everyone with access and the credited residents. The uploader is notified
  when processing has finished or failed.
- Only the uploader and administrators see a draft. A draft never published is deleted 30 days after
  the upload.

## Series

- Any resident may create a series and put their own videos in it. Titles are unique, ignoring case,
  and so is an episode number within a series' season.
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

## Decisions

- GAHKFlix is separate from the photo album, which keeps the videos it already holds.
- Uploads bypass the app server: a multi-gigabyte film over dorm wifi cannot hold a web worker.
- Two MP4 renditions and a quality switch rather than adaptive streaming: no extra player to ship,
  and nothing to break on a phone.
- The time windows run from the upload, but a draft is its uploader's own until published.
- Duplicates are checked across all videos, not only the uploader's: one film should exist once.
- Encodes run one at a time on the media worker. One that stops reporting is retried, at most three
  times, and then marked failed for the uploader to see.
