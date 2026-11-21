<?php

// Runs after every successful login, once MediaWiki has the account (creating it on a first login):
// ties it to the resident GahkAuthLogin authenticated, and copies their e-mail and real name onto it.
// Replaces GahkAuthChangeName, which renamed accounts by overwriting user_name here — past the point
// where a proper rename (which resets the session token) can still happen; WikiAccount::prepareLogin
// renames before the account is loaded instead.

namespace Gahk;

use MediaWiki\Auth\AbstractSecondaryAuthenticationProvider;
use MediaWiki\Auth\AuthenticationResponse;

class GahkAccountSync extends AbstractSecondaryAuthenticationProvider
{
    public function beginSecondaryAuthentication($user, array $reqs)
    {
        $resident = $this->manager->getAuthenticationSessionData(GahkAuthLogin::RESIDENT);
        if (!is_array($resident)) {
            return AuthenticationResponse::newAbstain();
        }
        WikiAccount::sync($user, $resident);
        return AuthenticationResponse::newPass();
    }

    public function getAuthenticationRequests($action, array $options)
    {
        return [];
    }

    public function beginSecondaryAccountCreation($user, $creator, array $reqs)
    {
        return AuthenticationResponse::newPass();
    }
}
