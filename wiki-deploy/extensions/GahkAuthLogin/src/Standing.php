<?php

// A resident's standing (ResidentDirectory::standing) as the wiki uses it on every request: asked of
// the intranet's database at most every few minutes, and remembered for when it cannot answer.
//
//   - fresher than FRESH seconds          -> the remembered answer; no query at all.
//   - older, and the database answers     -> the new answer, remembered.
//   - older, and the database fails       -> the last answer, for up to KEEP seconds. Signed-in
//                                            residents carry on through an outage, admins included.
//   - nothing remembered, database fails  -> ResidentDirectoryUnavailable; the caller decides.
// After a failure the database is left alone for PAUSE seconds, so an outage costs one connection
// timeout every PAUSE seconds, not one per page load.
//
// Kept in the local server cache (APCu, shared by every Apache process in the container), not the
// session: standing belongs to the resident, and every session of theirs should see a change at once.
// A container restart empties it, which only matters if the database is down at that moment.

namespace Gahk;

use MediaWiki\MediaWikiServices;
use Wikimedia\ObjectCache\BagOStuff;

class Standing
{
    /** How long an answer is used without asking again: the delay before a change takes effect. */
    const FRESH = 300;

    /** How long the last answer stands in while the database cannot answer. */
    const KEEP = 86400;

    /** How long to leave the database alone after it failed to answer. */
    const PAUSE = 30;

    /**
     * @param int $residentId
     * @return array|null as ResidentDirectory::standing
     * @throws ResidentDirectoryUnavailable when the database fails and nothing is remembered
     */
    public static function of($residentId)
    {
        $cache = self::cache();
        $entry = $cache->get(self::key($cache, $residentId));
        if (is_array($entry) && time() - $entry['checked'] < self::FRESH) {
            return $entry['standing'];
        }

        if (!$cache->get($cache->makeKey('gahk-directory-paused'))) {
            try {
                return self::refresh($residentId);
            } catch (ResidentDirectoryUnavailable $e) {
                $cache->set($cache->makeKey('gahk-directory-paused'), 1, self::PAUSE);
                Log::get()->warning('GahkAuthLogin: resident database unavailable, using the last known '
                    . 'standing for ' . self::PAUSE . ' s: ' . $e->getMessage());
            }
        }

        if (is_array($entry)) {
            return $entry['standing'];
        }
        throw new ResidentDirectoryUnavailable('database paused, and nothing known about resident ' . $residentId);
    }

    /**
     * Ask the database now and remember the answer. A login does this, so the account it has just
     * renamed or updated is never compared with what the cache knew before.
     *
     * @param int $residentId
     * @return array|null as ResidentDirectory::standing
     * @throws ResidentDirectoryUnavailable
     */
    public static function refresh($residentId)
    {
        $standing = ResidentDirectory::standing($residentId);
        $cache = self::cache();
        $cache->set(
            self::key($cache, $residentId),
            ['standing' => $standing, 'checked' => time()],
            self::KEEP
        );
        return $standing;
    }

    /**
     * @return BagOStuff
     */
    private static function cache()
    {
        return MediaWikiServices::getInstance()->getLocalServerObjectCache();
    }

    /**
     * @param BagOStuff $cache
     * @param int $residentId
     * @return string
     */
    private static function key($cache, $residentId)
    {
        return $cache->makeKey('gahk-standing', (int)$residentId);
    }
}
