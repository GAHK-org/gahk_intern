<?php

// A wiki session lasts only as long as the intranet still lets the resident in, and Netværksgruppen
// are the wiki's administrators only as long as they are Netværksgruppen. Nothing in the intranet
// tells the wiki when either changes, so every request looks at the resident's standing — which
// Standing asks of the intranet's database at most every five minutes, and remembers for outages.
//
// Each request, for the account behind the session (onSessionCheckInfo):
//   - not tied to a resident (a session from before this shipped)  -> dropped; logging in links it.
//   - resident deactivated, deleted, or the kitchen account         -> dropped, and every other
//     session and "remember me" cookie of the account with it.
//   - its name, e-mail or real name no longer matches the intranet  -> dropped; logging in again
//     renames and updates the account (WikiAccount).
// When the intranet's database cannot answer, the last known standing stands in, admin rights
// included: an outage there should not sign everyone out of the wiki. Someone the wiki knows nothing
// about yet keeps their session without admin rights. Nobody can log in meanwhile.
//
// Admin rights are *implicit* groups (onUserEffectiveGroups), never written to user_groups, so they
// cannot outlive the role. The flip side is that Special:ListUsers does not show them. Groups granted
// by hand in the wiki are left alone.

namespace Gahk;

use MediaWiki\Deferred\DeferredUpdates;
use MediaWiki\MediaWikiServices;
use MediaWiki\Session\SessionManager;
use MediaWiki\User\User;

class GahkSession
{
    /** @var array<int, bool> per wiki user id: are they an admin, for this request */
    private static $admin = [];

    public static function onSessionCheckInfo(&$reason, $info, $request, $metadata, $data)
    {
        $userInfo = $info->getUserInfo();
        if ($userInfo === null || $userInfo->isAnon() || !self::enforced()) {
            return true;
        }
        $uid = $userInfo->getId();
        $account = WikiAccount::load($uid);
        if ($account === null) {
            return true; // No such account: MediaWiki rejects the session itself.
        }
        if ($account['resident'] === null) {
            $reason = 'GahkSession: account not tied to a resident; log in again';
            return false;
        }

        try {
            $standing = Standing::of($account['resident']);
        } catch (ResidentDirectoryUnavailable $e) {
            self::$admin[$uid] = false;
            return true;
        }

        if ($standing === null || !$standing['allowed']) {
            $reason = "GahkSession: resident {$account['resident']} may no longer use the wiki";
            // After the request: invalidating saves the account, which is no job for a session check.
            DeferredUpdates::addCallableUpdate(static function () use ($uid) {
                SessionManager::singleton()->invalidateSessionsForUser(User::newFromId($uid));
            });
            return false;
        }

        if ($account['name'] !== WikiAccount::nameFor($standing['email'])
            || $account['email'] !== trim($standing['email'])
            || $account['real_name'] !== WikiAccount::realNameFor($standing)
        ) {
            $reason = 'GahkSession: account out of date with the intranet; log in again';
            return false;
        }

        self::$admin[$uid] = $standing['admin'];
        return true;
    }

    public static function onUserEffectiveGroups($user, &$groups)
    {
        if (!$user->isRegistered() || !self::enforced()) {
            return;
        }
        $uid = $user->getId();
        if (!array_key_exists($uid, self::$admin)) {
            // Someone other than the session's user (or a request without a session check).
            self::$admin[$uid] = self::isAdmin($uid);
        }
        if (self::$admin[$uid]) {
            // sysop: the sidebar and other MediaWiki: pages, protection, deletion, blocks.
            // interface-admin: site-wide CSS and JavaScript.
            $groups[] = 'sysop';
            $groups[] = 'interface-admin';
        }
    }

    /**
     * @param int $uid
     * @return bool
     */
    private static function isAdmin($uid)
    {
        $account = WikiAccount::load($uid);
        if ($account === null || $account['resident'] === null) {
            return false;
        }
        try {
            $standing = Standing::of($account['resident']);
        } catch (ResidentDirectoryUnavailable $e) {
            return false;
        }
        return $standing !== null && $standing['allowed'] && $standing['admin'];
    }

    /**
     * Only while residents log in through GahkAuthLogin. The break-glass procedure (CUTOVER.md
     * Phase 5A) comments that provider out to fall back on local passwords, and those accounts are
     * tied to no resident: checking them would sign the break-glass admin straight out again.
     *
     * @return bool
     */
    private static function enforced()
    {
        $auth = MediaWikiServices::getInstance()->getMainConfig()->get('AuthManagerAutoConfig');
        return isset($auth['primaryauth']['gahk']);
    }
}
