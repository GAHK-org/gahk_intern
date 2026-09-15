# Feature: Media album
Photo and video album functionality.

Hereafter "media" will refer to either a photo or a video.

Each photo and video should exist should exist in original upload, compressed high definition and thumbnail.
Store metadata such as original date, location, camera type, uploaded by user etc...
Retrieve metadata from photos/videos on upload if possible.

Use the S3 bucket for binary blobs (original, compressed and thumbnail).

All photos/videos must belong to exactly one album.

Albums will be locked if no media has been added for 3 months.
Locked albums cannot have new media uploaded nor media deleted.

Each album must belong to a folder/group.
Folder/group names are either the year yyyy, or other "Andet".
Albums within the same folder/group cannot have the same name.


## Creating albums and uploading/editing photos
Only users that are member of fotogruppen or admins can create albums and upload media.

Other users may upload to albums, but the uploads must be approved by a member of fotogruppen or admin before it's a part of the album.
If an upload is rejected, it will be moved to the bin.
If the upload is not approved within 30 days, it will be deleted permanently.
Pending media will be shown only to members of fotogruppen, admins and the user that requested upload.
Pending media will be displayed with text "Afventer godkendelse" over the thumbnail.


## Deleting
Photos/videos that are uploaded more than 30 days ago, cannot be deleted.

Only users that are member of fotogruppen or admins can delete.

If a photo/video is deleted, it's moved to a bin such that it's not instantly deleted. Media in the bin can be restored at any time. Media can only deleted after being in the bin for more than 30 days.

If the media is uploaded less than 1 hour ago, it can be deleted instantly.

An album can only be deleted if it's empty. Albums can't be renamed.

## UX
When an media is clicked, open a modal showing the high definition version.
Navigate to previous/next with arrows on each side of the media or arrow keys.
Display metadata to the right when an media is clicked.
A user should be able to download the original upload.
