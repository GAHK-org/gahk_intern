# Login og profil

How residents get into the members area, manage their password and edit their own profile.

## Login and logout
- Residents log in at `/intern/admin/login` with their email and password. After logging in they land on the members area or on the page they were trying to reach.
- The email is not case-sensitive.
- Wrong credentials show one generic error, without saying whether the email exists.
- Deactivated residents cannot log in.
- Residents migrated from the old site log in with their old password, which is upgraded to modern hashing on their first login. Nobody is forced to reset.
- Roles are not fixed when someone logs in. They are checked on every page against the active month (see Administration).
- Log ud is in the account menu under the avatar in the top bar, beside Min profil, Notifikationsindstillinger and Skift kodeord.
- Logging out ends the session, including any role preview, and returns to the login page.

## Password change and reset
- A logged-in resident can change their password by entering the old one and the new one twice.
- New passwords must not be too short, too common, all digits or too similar to the resident's own details.
- "Glemt kodeord" emails a link for choosing a new password. The link expires after 2 hours and stops working once the password has been changed.
- The reset page always says a link was sent, whether or not the email belongs to anyone.
- Reset also works for residents who have never set a password, but not for deactivated residents.

## Welcome mail
- When Indstillingen adds a new resident to the monthly list, the account is created without a password, and the resident is emailed a "Velkommen til GAHK Intern" message.
- The welcome mail contains a link for setting a password, which expires after 2 hours like a reset link. It also links to "glemt kodeord" in case the first link has expired.
- If the welcome mail cannot be sent, the resident is still created. Indstillingen is warned and asked to point the resident to "glemt kodeord".

## Profiles
- Any logged-in resident can view any resident's profile page, reached from the alumneliste and from authors' names and avatars in Den Hurtige and on opslagstavlen. It shows:
  - picture (which opens full size), study, bio, Facebook and Instagram links;
  - room, embedsgruppe and cleaning group for the active month;
  - fylgje, birthday, move-in date, phone and email;
  - badges, and their 10 most recent posts on the notice board.
- Residents can edit their own profile picture (an image of at most 5 MB, or a HEIC photo of up to 25 MB that is stored as JPEG; it can also be removed), a bio of at most 500 characters, a Facebook link and an Instagram username.
- Residents cannot change their own name, email, phone or other details. Only Indstillingen can edit those.

## Decisions
- Reset links work for residents who have never set a password. Otherwise a new resident who missed the 2-hour welcome link would be told a link had been sent but never receive one. This is safe because the link only ever goes to the account's own email address.
- Login ignores letter case in the email because residents who typed a capital first letter could not log in.
- Old passwords are upgraded when residents log in, instead of forcing everyone to reset their password when the site moved.
- A failed welcome mail is reported, not swallowed. A wrong sender address must not look like success.
