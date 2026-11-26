<?php

// Read-only lookup of residents in the Django app's PostgreSQL database.
//
// Replaces the old mysqli connection to `intern_alumne` on the legacy MySQL box. That table no longer
// exists after the 2026-08 cutover, and the alternative — freezing a copy of it into the wiki's own
// database — would have kept the pre-breach SHA-256 hashes valid on the wiki forever, defeating the
// forced password reset. Reading the live Postgres row means a resident's current password is the one
// that works, and a reset takes effect on the wiki immediately.
//
// Credentials come from the environment; nothing is hardcoded. Use a dedicated read-only Postgres
// role, not the application's own `gahk` user:
//
//   CREATE ROLE wiki_ro LOGIN PASSWORD '...';
//   GRANT CONNECT ON DATABASE gahk TO wiki_ro;
//   GRANT USAGE ON SCHEMA public TO wiki_ro;
//
// The column grants on the residents tables come from the intranet's migration
// residents/0013_wiki_ro_grants, which applies them whenever a role of that name exists. Grant by hand
// only if the role is created after that migration ran (the statements are in the migration).
//
// Environment: GAHK_PG_HOST, GAHK_PG_PORT, GAHK_PG_DB, GAHK_PG_USER, GAHK_PG_PASSWORD
// Requires the pdo_pgsql PHP extension.

namespace Gahk;

use PDO;
use PDOException;

class ResidentDirectory
{
    /** @var PDO|null */
    private static $pdo = null;

    /** @var bool the connection already failed in this request: do not wait for it twice */
    private static $unreachable = false;

    /**
     * @return PDO|null null when the database is unreachable or unconfigured
     */
    private static function connect()
    {
        if (self::$pdo !== null) {
            return self::$pdo;
        }
        if (self::$unreachable) {
            return null;
        }

        $host = getenv('GAHK_PG_HOST');
        $db = getenv('GAHK_PG_DB');
        $user = getenv('GAHK_PG_USER');
        $password = getenv('GAHK_PG_PASSWORD');
        $port = getenv('GAHK_PG_PORT') ?: '5432';

        if (!$host || !$db || !$user) {
            Log::get()->warning('GahkAuthLogin: GAHK_PG_* environment is not configured; cannot authenticate.');
            return null;
        }

        try {
            self::$pdo = new PDO(
                // Two seconds, libpq's shortest: a database that does not answer at all should cost a
                // page little, and Standing pauses further attempts after one has failed.
                sprintf('pgsql:host=%s;port=%s;dbname=%s;connect_timeout=2', $host, $port, $db),
                $user,
                $password !== false ? $password : '',
                [
                    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                    PDO::ATTR_TIMEOUT => 2,
                ]
            );
        } catch (PDOException $e) {
            // Never surface connection details to the visitor.
            Log::get()->warning('GahkAuthLogin: cannot reach the resident database: ' . $e->getMessage());
            self::$unreachable = true;
            return null;
        }

        return self::$pdo;
    }

    /**
     * Look a resident up by e-mail, or by numeric id (the legacy provider accepted either), for the
     * login form. Whether they may use the wiki is standing()'s answer, not this one's.
     *
     * @param string $identifier
     * @return array|null  ['id', 'email', 'password', 'first_name', 'last_name'] for an active
     *     resident, else null
     * @throws ResidentDirectoryUnavailable when the database cannot answer
     */
    public static function find($identifier)
    {
        $pdo = self::connect();
        if ($pdo === null) {
            throw new ResidentDirectoryUnavailable('not connected');
        }

        // Parameterised — the submitted value is never concatenated into SQL. This is the injection
        // that the original extension shipped with.
        $sql = 'SELECT id, email, password, first_name, last_name
                  FROM residents_resident
                 WHERE is_active AND (lower(email) = lower(:ident)';
        $params = [':ident' => $identifier];

        if (ctype_digit((string)$identifier)) {
            $sql .= ' OR id = :ident_id';
            $params[':ident_id'] = (int)$identifier;
        }
        $sql .= ') LIMIT 1';

        try {
            $stmt = $pdo->prepare($sql);
            $stmt->execute($params);
            $row = $stmt->fetch();
        } catch (PDOException $e) {
            throw new ResidentDirectoryUnavailable($e->getMessage(), 0, $e);
        }

        return $row !== false ? $row : null;
    }

    /**
     * Everything the wiki needs to know about a resident right now: may they use the wiki, what
     * should their account say, are they an admin. Asked through Standing, which caches it.
     *
     * Mirrors the intranet's own rules. Access is `is_active`, as for logging in to the intranet;
     * the shared kitchen login is not a resident and gets no wiki. Netvaerksgruppen is the
     * `administrator` role in the ACTIVE month (the newest residency list that has already started,
     * or the calendar month when there is none — residents.models.active_period) AND a second
     * administrator's approval (an approved AdminAccessGrant), as for the intranet's Django admin;
     * a Django superuser counts as one.
     *
     * @param int $id the resident's id (residents_resident.id)
     * @return array|null null when no such resident exists, else ['email', 'first_name',
     *     'last_name', 'allowed' => bool, 'admin' => bool]
     * @throws ResidentDirectoryUnavailable when the database cannot answer
     */
    public static function standing($id)
    {
        $pdo = self::connect();
        if ($pdo === null) {
            throw new ResidentDirectoryUnavailable('not connected');
        }

        $today = new \DateTimeImmutable('now', new \DateTimeZone('Europe/Copenhagen'));
        $year = (int)$today->format('Y');
        $month = (int)$today->format('n');

        // The fallback row sorts last, so the calendar month is used only when no list has started.
        $sql = "WITH active AS (
                    SELECT year, month FROM (
                        (SELECT year, month, 0 AS fallback
                           FROM residents_residency
                          WHERE (year, month) <= (CAST(:year AS smallint), CAST(:month AS smallint))
                          ORDER BY year DESC, month DESC
                          LIMIT 1)
                        UNION ALL
                        SELECT CAST(:cal_year AS smallint), CAST(:cal_month AS smallint), 1
                    ) candidates
                    ORDER BY fallback
                    LIMIT 1
                )
                SELECT r.email, r.first_name, r.last_name,
                       r.is_active AND NOT r.is_kitchen AS allowed,
                       r.is_active AND (
                           r.is_superuser
                           OR (EXISTS (SELECT 1 FROM residents_roleassignment ra
                                         JOIN active a ON ra.year = a.year AND ra.month = a.month
                                        WHERE ra.resident_id = r.id AND ra.role = 'administrator')
                               AND EXISTS (SELECT 1 FROM residents_adminaccessgrant g
                                            WHERE g.resident_id = r.id AND g.status = 'approved'))
                       ) AS admin
                  FROM residents_resident r
                 WHERE r.id = :id";

        try {
            $stmt = $pdo->prepare($sql);
            $stmt->execute([
                ':id' => (int)$id,
                ':year' => $year,
                ':month' => $month,
                ':cal_year' => $year,
                ':cal_month' => $month,
            ]);
            $row = $stmt->fetch();
        } catch (PDOException $e) {
            // Also what missing column grants (residents/0013_wiki_ro_grants) look like.
            throw new ResidentDirectoryUnavailable($e->getMessage(), 0, $e);
        }
        if ($row === false) {
            return null;
        }

        $row['allowed'] = (bool)$row['allowed'];
        $row['admin'] = (bool)$row['admin'];
        return $row;
    }
}
