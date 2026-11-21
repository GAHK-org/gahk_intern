<?php

// Verification of Django password hashes from PHP.
//
// The wiki authenticates against the Django app's `residents_resident.password` column, so it has to
// understand Django's hash formats. Verify only — nothing here ever writes a hash.
//
// Formats in play (app/config/settings.py PASSWORD_HASHERS, app/core/hashers.py):
//
//   pbkdf2_sha256$<iterations>$<salt>$<base64>   default; every reset password becomes this
//   pbkdf2_sha1$<iterations>$<salt>$<base64>     listed for compatibility
//   gahk_sha256$$<hex>                           legacy unsalted SHA-256 from the PHP site
//   !<anything>                                  unusable — must never authenticate
//
// scrypt$... is listed in PASSWORD_HASHERS but PHP 7.4 has no scrypt, so it is explicitly refused
// rather than silently mishandled. No resident currently has one.

namespace Gahk;

class DjangoPasswords
{
    /**
     * @param string $password plaintext submitted at the login form
     * @param string $encoded  the stored Django hash
     * @return bool            true only on a definite match
     */
    public static function check($password, $encoded)
    {
        if (!is_string($encoded) || $encoded === '') {
            return false;
        }
        // set_unusable_password() stores "!" followed by random text. Never authenticates.
        if ($encoded[0] === '!') {
            return false;
        }

        $parts = explode('$', $encoded);
        $algorithm = $parts[0];

        if ($algorithm === 'gahk_sha256') {
            // "gahk_sha256$$<hex>" — empty salt field, hence the two consecutive separators.
            if (count($parts) !== 3) {
                return false;
            }
            return hash_equals($parts[2], hash('sha256', $password));
        }

        if ($algorithm === 'pbkdf2_sha256' || $algorithm === 'pbkdf2_sha1') {
            if (count($parts) !== 4) {
                return false;
            }
            $digest = $algorithm === 'pbkdf2_sha256' ? 'sha256' : 'sha1';
            $iterations = (int)$parts[1];
            $salt = $parts[2];
            $expected = $parts[3];
            if ($iterations < 1) {
                return false;
            }
            // length 0 = the digest's native size, matching Django's default dklen.
            $computed = base64_encode(hash_pbkdf2($digest, $password, $salt, $iterations, 0, true));
            return hash_equals($expected, $computed);
        }

        // Unknown or unsupported algorithm (e.g. scrypt): fail closed.
        Log::get()->warning('GahkAuthLogin: unsupported password hash algorithm ' . $algorithm);
        return false;
    }
}
