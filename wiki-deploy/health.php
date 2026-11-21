<?php
# /healthz and /health for wiki.gahk.dk, the same pair as the intranet's (app/core/health.py).
#
#   * /healthz — liveness, for the Docker HEALTHCHECK in the Dockerfile, which Coolify reads. It only
#     says Apache and PHP are serving and touches nothing else, on purpose: Traefik stops routing to
#     a container Docker calls unhealthy, and a MariaDB restart should not also take the wiki's
#     error pages offline.
#   * /health — the whole wiki, for an external uptime monitor: MediaWiki's MariaDB and the
#     intranet's PostgreSQL, which every login reads (GahkAuthLogin). 200 when both answer, 503
#     otherwise, with a JSON body naming which check failed. The reason goes to the log, not the
#     body: the endpoint is public, and a connection error can carry hosts and user names.
#
# A script of its own rather than a MediaWiki page: the wiki is private ($wgGroupPermissions['*']
# ['read'] = false), so every page of it answers a probe with a redirect to the login, and loading
# MediaWiki would start a session for each one. apache-wiki.conf rewrites both paths here.

const TIMEOUT_SECONDS = 3; // Fail fast: a monitor that times out learns nothing about which part is down.

function check_wiki_database(): void
{
    // MW_DB_HOST may carry a port, as MediaWiki's $wgDBserver allows ("host:port").
    [$host, $port] = array_pad(explode(':', (string) getenv('MW_DB_HOST'), 2), 2, '3306');
    $db = mysqli_init();
    $db->options(MYSQLI_OPT_CONNECT_TIMEOUT, TIMEOUT_SECONDS);
    $db->real_connect(
        $host,
        getenv('MW_DB_USER') ?: 'wiki',
        (string) getenv('MW_DB_PASSWORD'),
        getenv('MW_DB_NAME') ?: 'gahk_wiki',
        (int) $port
    );
    $db->query('SELECT 1');
    $db->close();
}

function check_resident_database(): void
{
    $pdo = new PDO(
        sprintf(
            'pgsql:host=%s;port=%s;dbname=%s;connect_timeout=%d',
            getenv('GAHK_PG_HOST'),
            getenv('GAHK_PG_PORT') ?: '5432',
            getenv('GAHK_PG_DB'),
            TIMEOUT_SECONDS
        ),
        (string) getenv('GAHK_PG_USER'),
        (string) getenv('GAHK_PG_PASSWORD'),
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
    );
    $pdo->query('SELECT 1');
}

function guarded(string $name, callable $check): array
{
    try {
        $check();
        return ['ok' => true];
    } catch (Throwable $e) {
        error_log("gahk-health: check $name failed: " . $e->getMessage());
        return ['ok' => false];
    }
}

header('Cache-Control: no-store');

if (parse_url($_SERVER['REQUEST_URI'] ?? '', PHP_URL_PATH) === '/healthz') {
    header('Content-Type: text/plain');
    echo 'ok';
    return;
}

// Throw on every mysqli error rather than return false (the default since PHP 8.1, made explicit).
mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT);

$checks = [
    'database' => guarded('database', 'check_wiki_database'),
    'residents' => guarded('residents', 'check_resident_database'),
];
$ok = !in_array(false, array_column($checks, 'ok'), true);

http_response_code($ok ? 200 : 503);
header('Content-Type: application/json');
echo json_encode(['status' => $ok ? 'ok' : 'fail', 'checks' => $checks]);
