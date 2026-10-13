# Feature: notifications

Notifications may be sent for many reasons.
Currently, subscription to push notifications is scattered around the app.

Notifications should include both in-app notifications, as well as push notifications.
**Some notifications are push only, whilst other might be both.**

## In-app notifications
Include a notifications page and a notification dropdown in the nav bar.

The dropdown should show the 10 most recent notifications.

The page should show all active notifications.
For the page, add both a chronologic view and a view grouped by category.

### Seen/unseen
If a user has clicked a notification (in-app or push), mark the notification as seen.
Add a visual cue to indicate if a notification is seen or not.


## Categories
Current categories include (but not limited to):

- System
- General
- Events
- Chat (den hurtige)
    * Likes
    * Replies
- Posts
    * Replies
    * Likes


## Grouping

For notifications and push notifications, some are grouped, i.e., you will only see the most recent.
For instance, if you get a notification for "user-A" liked your message, then "user-B" likes the same message too, the initial notification will be replaced with "user-A and user-B liked your message". If more users like it, it might end up saying "x users liked your message".


## Settings page

Include a personal settings page for each user, where they can manage they may manage their notification settings.
These settings apply for the user, not the device.

On the settings page, users may toggle if they want to receive notifications for different categories.

Enable separate toggle for each chat room (den hurtige, tv-rezz, sportsman, gahkroom and m.h.g.a).
Add an indicator directly in each chat room, that indicates if the room is toggled on. Clicking the indicator takes the user to this section of the settings page.

For each category, the user may toggle for in-app notifications and push notifications separately.

By default, everything is enabled.


Include setting for vibration.
- On/off in general
- By category
- By importance level. E.g., don't vibrate for lowest importance level



## Prompt for subscription to push notifications
Subscription to push notifications should be once per device, i.e., not for each different part of the app that sends notifications. A single point per device.

Prompt the user to subscribe at the dashboard, at the chat (den hurtige) and in the settings page.


## TTL
When notifications are created, they should have a TTL.
For time limited messages in the chat, set the TTL to the message expiration time.

For other notifications, make the default TTL 7 days.


## Importance

Add 4 levels of importance to notifications.
In the dropdown and on the notifications page, show the most important first, and with visual cues that they are important (color/icons etc...).

Allow the most important notification level to overrule notification settings. That is, a user will still receive a notification, even though the category is disabled.

Example of lowest importance is "user liked your message".


## Admin page

On the admin page, admins should be able to view active push subscriptions for each user.
Enable admin to cancel a subscription.

Show statistics and metrics for notifications.


