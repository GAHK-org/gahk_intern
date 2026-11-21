<?php

// The extension's log channel. Not wfLogWarning: that raises a PHP warning, and with display_errors
// on, PHP prints it into the response — on every page, once the per-request resident check
// (GahkSession) runs into an unreachable database, which breaks the API's JSON and load.php's CSS
// and JavaScript. LocalSettings.php sends this channel to the container log.

namespace Gahk;

use MediaWiki\Logger\LoggerFactory;
use Psr\Log\LoggerInterface;

class Log
{
    /**
     * @return LoggerInterface
     */
    public static function get()
    {
        return LoggerFactory::getInstance('GahkAuthLogin');
    }
}
