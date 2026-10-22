<?php

/**
 * Code for the @ref Extensions-ShowRealUsernames.
 *
 * @file
 *
 * @ingroup Extensions
 * @ingroup Extensions-ShowRealUsernames
 *
 * @author Paul Lustgarten
 * @author John Erling Blad
 * @author [RV1971](https://www.mediawiki.org/wiki/User:RV1971)
 */

use MediaWiki\MediaWikiServices;

/**
 * Class implementing the @ref Extensions-ShowRealUsernames.
 *
 * @ingroup Extensions-ShowRealUsernames
 */

class ShowRealUsernames {

	/**
	 * SpecialListusersFormatRow hook handler
	 * @see https://www.mediawiki.org/wiki/Manual:Hooks/SpecialListusersFormatRow
	 *
	 * Augment the display of a row to include the user's real name.
	 *
	 * @param string &$item HTML to be returned. Will be wrapped in
	 * &lt;li&gt;&lt;/li&gt; after the hook finishes. We assume that $item is
	 * non-null and begins with an &lt;a&gt; element.
	 *
	 * @param stdClass $row Database row object.
	 *
	 * @return bool Always TRUE.
	 */
	public static function onSpecialListusersFormatRow( &$item, $row ) {
		$context = RequestContext::getMain();

		if ( $row->user_real_name === ''
			|| !$context->getUser()->isAllowed( 'showrealname' ) ) {
			return true;
		}

		$showRealUsernamesFields = $context->getConfig()->get( 'ShowRealUsernamesFields' );
		$showRealUsernamesInline = $context->getConfig()->get( 'ShowRealUsernamesInline' );

		$values = [];
		foreach ( $showRealUsernamesFields as $field ) {
			$values[] = htmlspecialchars( $row->$field );
		}

		$m = [];
		if ( preg_match( '/^(.*?<a\b[^>]*>)([^<]*)(<\/a\b[^>]*>)(.*)$/',
				$item, $m ) ) {
			$fields = implode( $context->msg( 'pipe-separator' )->text(), $values );
			if ( $showRealUsernamesInline ) {
				$item = $m[1]
					. $context->msg( 'sru-realname-inline', $fields )->text()
					. "{$m[3]}{$m[4]}";
			} else {
				$item = "{$m[1]}{$m[2]}{$m[3]}"
					. $context->msg( 'sru-realname-append', $fields )->text()
					. $m[4];
			}
		}

		return true;
	}

	/**
	 * SpecialListusersQueryInfo hook handler
	 * @see https://www.mediawiki.org/wiki/Manual:Hooks/SpecialListusersQueryInfo
	 *
	 * Augment the DB query for each user to also fetch their real
	 * name and/or other fields.
	 *
	 * @param UsersPager $pager The UsersPager instance.
	 *
	 * @param array &$query The query array to be returned.
	 *
	 * @return bool Always TRUE.
	 */
	public static function onSpecialListusersQueryInfo( UsersPager $pager, array &$query ) {
		$config = MediaWikiServices::getInstance()->getMainConfig();
		$showRealUsernamesFields = $config->get( 'ShowRealUsernamesFields' );

		// Add extra fields.
		$query['fields'] += array_combine( $showRealUsernamesFields, $showRealUsernamesFields );

		$query['options']['GROUP BY'] = array_merge(
			(array)$query['options']['GROUP BY'],
			array_diff(
				$showRealUsernamesFields,
				(array)$query['options']['GROUP BY'] ) );

		return true;
	}
}
