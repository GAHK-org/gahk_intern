<?php

// One wiki account per resident, named after their e-mail address, and kept that way.
//
// The account is tied to the resident by a hidden user option holding their id (LINK), so it is
// found again after the e-mail changes in the intranet and renamed to match — history, watchlist and
// user pages go with it. Users cannot set that option themselves: the API refuses options nobody
// registered, and resetting preferences only removes the link, which the next login restores.
//
// Accounts made by the old login are named after whatever the person typed (their e-mail in any
// capitalisation, or their numeric id), and were never linked. The first login after this shipped
// adopts one of them: the one already named right, else the one with the most edits.

namespace Gahk;

use MediaWiki\Deferred\DeferredUpdates;
use MediaWiki\MediaWikiServices;
use MediaWiki\RenameUser\RenameuserSQL;
use MediaWiki\Title\Title;
use MediaWiki\User\User;
use MediaWiki\User\UserNameUtils;
use Wikimedia\Rdbms\IDBAccessObject;

class WikiAccount
{
    /** User option holding the resident id (residents_resident.id) the account belongs to. */
    const LINK = 'gahk-resident-id';

    /** The system account renames and page moves are logged under. */
    const RENAMER = 'Intranettet';

    /**
     * The account name for an e-mail address: lowercased, then MediaWiki's canonical form (first
     * letter capitalised, underscores as spaces).
     *
     * @param string $email
     * @return string|null null when MediaWiki cannot use it as a user name
     */
    public static function nameFor($email)
    {
        $name = MediaWikiServices::getInstance()->getUserNameUtils()
            ->getCanonical(mb_strtolower(trim($email)), UserNameUtils::RIGOR_USABLE);
        return $name === false ? null : $name;
    }

    /**
     * The real name the account should carry.
     *
     * @param array $resident with 'first_name' and 'last_name'
     * @return string
     */
    public static function realNameFor(array $resident)
    {
        return trim($resident['first_name'] . ' ' . $resident['last_name']);
    }

    /**
     * Make sure the resident's account carries the name they are about to log in under, and return
     * that name. Runs before MediaWiki loads the account, because a rename resets the account's
     * session token: done any later, it would invalidate the very session being created.
     *
     * @param array $resident with 'id' and 'email'
     * @return string|null the account name, or null if it could not be made ready (logged)
     */
    public static function prepareLogin(array $resident)
    {
        $name = self::nameFor($resident['email']);
        if ($name === null) {
            Log::get()->warning("GahkAuthLogin: resident {$resident['id']} has an e-mail MediaWiki cannot use as a name");
            return null;
        }

        $uid = self::linkedAccount($resident['id']) ?: self::legacyAccount($resident['id'], $name);
        if (!self::clearName($name, $uid)) {
            return null;
        }
        if ($uid) {
            $current = User::newFromId($uid)->getName();
            if ($current !== $name && !self::rename($uid, $current, $name, 'E-mailadressen er ændret i intranettet')) {
                return null;
            }
        }
        // No account yet: MediaWiki creates one under this name when the login passes.
        return $name;
    }

    /**
     * After a login: tie the account to the resident and bring its e-mail and real name up to date.
     * The e-mail counts as confirmed, since it is the address the intranet already uses.
     *
     * @param User $user the account that just logged in
     * @param array $resident with 'id', 'email', 'first_name', 'last_name'
     */
    public static function sync(User $user, array $resident)
    {
        $options = MediaWikiServices::getInstance()->getUserOptionsManager();
        $options->setOption($user, self::LINK, (string)$resident['id']);
        $options->saveOptions($user);

        $email = trim($resident['email']);
        $user->setRealName(self::realNameFor($resident));
        $user->setEmail($email);
        if (!$user->isEmailConfirmed()) {
            $user->setEmailAuthenticationTimestamp(wfTimestampNow());
        }
        $user->saveSettings();
    }

    /**
     * The account as the session check sees it, in one query.
     *
     * @param int $uid
     * @return array|null ['name', 'real_name', 'email', 'resident' => int|null], or null for no
     *     such account
     */
    public static function load($uid)
    {
        $dbr = MediaWikiServices::getInstance()->getConnectionProvider()->getReplicaDatabase();
        $row = $dbr->newSelectQueryBuilder()
            ->select(['user_name', 'user_real_name', 'user_email', 'up_value'])
            ->from('user')
            ->leftJoin('user_properties', null, ['up_user = user_id', 'up_property' => self::LINK])
            ->where(['user_id' => (int)$uid])
            ->caller(__METHOD__)
            ->fetchRow();
        if (!$row) {
            return null;
        }
        return [
            'name' => $row->user_name,
            'real_name' => $row->user_real_name,
            'email' => $row->user_email,
            'resident' => ctype_digit((string)$row->up_value) ? (int)$row->up_value : null,
        ];
    }

    /**
     * @param int $residentId
     * @return int the account linked to the resident, or 0
     */
    private static function linkedAccount($residentId)
    {
        $dbw = MediaWikiServices::getInstance()->getConnectionProvider()->getPrimaryDatabase();
        return (int)$dbw->newSelectQueryBuilder()
            ->select('up_user')
            ->from('user_properties')
            ->where(['up_property' => self::LINK, 'up_value' => (string)$residentId])
            ->orderBy('up_user')
            ->limit(1)
            ->caller(__METHOD__)
            ->fetchField();
    }

    /**
     * An unlinked account the old login made for this resident.
     *
     * @param int $residentId
     * @param string $name the account name they should have
     * @return int the account, or 0
     */
    private static function legacyAccount($residentId, $name)
    {
        $dbw = MediaWikiServices::getInstance()->getConnectionProvider()->getPrimaryDatabase();
        // user_name is binary, so compare a converted copy to match any capitalisation.
        $candidates = $dbw->makeList([mb_strtolower($name), (string)$residentId]);
        return (int)$dbw->newSelectQueryBuilder()
            ->select('user_id')
            ->from('user')
            ->leftJoin('user_properties', null, ['up_user = user_id', 'up_property' => self::LINK])
            ->where(['up_user' => null])
            ->andWhere("LOWER(CONVERT(user_name USING utf8mb4)) IN ($candidates)")
            ->orderBy(['user_name = ' . $dbw->addQuotes($name), 'user_editcount'], 'DESC')
            ->limit(1)
            ->caller(__METHOD__)
            ->fetchField();
    }

    /**
     * Make $name free for account $uid: an account of any other id that holds it is renamed aside
     * to "<name> (konto <id>)". That is an old duplicate of this resident, or the account of someone
     * who used this address before — whose next login renames it to their own address anyway.
     *
     * @param string $name
     * @param int $uid 0 when the resident has no account yet
     * @return bool false if the name is taken and could not be freed (logged)
     */
    private static function clearName($name, $uid)
    {
        $holder = User::newFromName($name, false);
        $holderId = $holder ? $holder->idForName(IDBAccessObject::READ_LATEST) : 0;
        if ($holderId === 0 || $holderId === (int)$uid) {
            return true;
        }
        return self::rename($holderId, $name, "$name (konto $holderId)", 'Navnet bruges nu af en anden beboer');
    }

    /**
     * Rename an account the way Special:RenameUser does, user page and talk page included.
     *
     * @param int $uid
     * @param string $old
     * @param string $new
     * @param string $reason
     * @return bool
     */
    private static function rename($uid, $old, $new, $reason)
    {
        $renamer = User::newSystemUser(self::RENAMER, ['steal' => false]);
        if ($renamer === null) {
            Log::get()->warning('GahkAuthLogin: cannot rename accounts: the name "' . self::RENAMER . '" belongs to a person');
            return false;
        }
        if (!(new RenameuserSQL($old, $new, $uid, $renamer, ['reason' => $reason]))->rename()) {
            Log::get()->warning("GahkAuthLogin: renaming account $uid from \"$old\" to \"$new\" failed");
            return false;
        }

        // The user pages follow after the login has been saved, in a transaction of their own: a
        // move that fails must not take the login down with it. (It happened in testing: the first
        // redirect a wiki ever makes registers its change tag on another connection, and MariaDB
        // 11.6+ then refuses the login's own transaction — error 1020.) A failed move leaves the
        // pages under the old name, where they still work; Special:MovePage can finish the job.
        DeferredUpdates::addCallableUpdate(static function () use ($renamer, $old, $new, $reason) {
            $movePages = MediaWikiServices::getInstance()->getMovePageFactory();
            foreach ([NS_USER, NS_USER_TALK] as $ns) {
                $from = Title::makeTitleSafe($ns, $old);
                $to = Title::makeTitleSafe($ns, $new);
                if ($from === null || $to === null) {
                    continue;
                }
                // Redirects from the old names keep links to them (signatures) working.
                $move = $movePages->newMovePage($from, $to);
                if ($from->exists()) {
                    $move->move($renamer, $reason, true);
                }
                $move->moveSubpages($renamer, $reason, true);
            }
        });
        return true;
    }
}
