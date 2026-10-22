<?php

namespace MediaWiki\Extension\Realnames;

/*
	Copyright 2011-2019 Finlay Beaton. All rights reserved.

	Redistribution and use in source and binary forms, with or without modification, are
	permitted provided that the following conditions are met:

		1. Redistributions of source code must retain the above copyright notice, this list of
			conditions and the following disclaimer.

		2. Redistributions in binary form must reproduce the above copyright notice, this list
			of conditions and the following disclaimer in the documentation and/or other materials
			provided with the distribution.

	THIS SOFTWARE IS PROVIDED BY Finlay Beaton ''AS IS'' AND ANY EXPRESS OR IMPLIED
	WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
	FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL Finlay Beaton OR
	CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
	CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
	SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
	ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
	NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
	ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
*/

use MediaWiki\Config\Config;
use MediaWiki\Language\Language;
use MediaWiki\Output\OutputPage;
use MediaWiki\User\User;
use MediaWiki\User\UserFactory;
use Skin;

/**
 * >= 0.1
 */
class Realnames implements
	\MediaWiki\Output\Hook\BeforePageDisplayHook,
	\MediaWiki\Hook\SkinTemplateNavigation__UniversalHook
{

	/**
	 * A cache of realnames for given users.
	 *
	 * @var array
	 * @since 2011-09-16, 0.1
	 */
	protected static array $realnames = [];

	/**
	 * namespace regex option string.
	 *
	 * @var string|null
	 * @since 2011-09-16, 0.2
	 */
	protected static ?string $namespacePrefixes = null;

	/**
	 * namespace regex option string urlencoded.
	 *
	 * @var string|null
	 * @since 2019-03-10, 0.6
	 */
	protected static ?string $namespacePrefixesEncoded = null;

	private Config $config;
	private Language $lang;
	private UserFactory $userFactory;

	public function __construct(
		Config $config,
		Language $lang,
		UserFactory $userFactory
	) {
		$this->config = $config;
		$this->lang = $lang;
		$this->userFactory = $userFactory;
	}

	/**
	 * checks a data set to see if we should proceed with the replacement.
	 *
	 * @param array $matches keyed with regex matches
	 *
	 * @return string text to replace the match with
	 *
	 * @since 2011-09-16, 0.1
	 * @see   lookForBare() for regex
	 */
	protected function checkBare( array $matches ): string {
		// matches come from self::lookForBare()'s regular expression
		$m = [
			'all' => $matches[0],
			'username' => $matches[1],
		];

		self::debug( __METHOD__, print_r( $m, true ) );

		// we do not currently do any checks on Bare replacements, a User: find is
		// always valid but we could add one in the future, and the debug
		// information is still convenient and keeps things consistent with checkLink
		return $this->replace( $m );
	}

	/**
	 * checks a data set to see if we should proceed with the replacement.
	 *
	 * @param array $matches keyed with regex matches
	 *
	 * @return string text to replace the match with
	 *
	 * @since 2011-09-16, 0.1
	 * @see   lookForBare() for regex
	 */
	protected function checkLink( array $matches ): string {
		// matches come from self::lookForLinks()'s regular expression
		$m = [
			'all' => $matches[0],
			'linkstart' => $matches[1],
			'linkuser' => $matches[2],
			'username' => $matches[3],
			'linkend' => $matches[4],
		];

		self::debug( __METHOD__, print_r( $m, true ) );

		// some links point to user pages but do not display the username,
		// we can safely ignore those
		// we need to urldecode the link for accents and special characters,
		// and ensure our username has underscores instead of spaces to match our link
		// before being able to do the comparison.
		if ( urldecode( $m['linkuser'] ) !== str_replace( ' ', '_', $m['username'] ) ) {
			return $m['all'];
		}

		return $this->replace( $m );
	}

	/**
	 * Outputs to the debug channel.
	 *
	 * @param string $method method name, usually __METHOD__
	 * @param string $text text to log
	 *
	 * @return void
	 *
	 * @since 2019-01-24, 0.3.2
	 */
	protected static function debug( string $method, string $text ): void {
		wfDebugLog( 'realnames', $method . ': ' . $text );
	}

	/**
	 * formats the final string in the configured style to display the real name.
	 *
	 * @param array $m keyed with strings called
	 *    \li<em>linkstart</em>
	 *    \li<em>username</em>
	 *    \li<em>realname</em>
	 *    \li<em>linkend</em>
	 *
	 * @return string formatted text to replace the match with
	 *
	 * @since 2011-09-16, 0.1
	 * @see   $wgRealnamesLinkStyle
	 * @see   $wgRealnamesBareStyle
	 * @see   $wgRealnamesStyles
	 * @see   $wgRealnamesBlank
	 */
	protected function display( array $m ): string {
		// what kind of formatting will we do?
		$style = $this->config->get( 'RealnamesLinkStyle' );
		$styleBlankName = $this->config->get( 'RealnamesLinkStyleBlankName' );
		$styleSameName = $this->config->get( 'RealnamesLinkStyleSameName' );
		if ( !isset( $m['linkstart'] ) ) {
			if ( $this->config->get( 'RealnamesBareStyle' ) ) {
				$style = $this->config->get( 'RealnamesBareStyle' );
			}
			if ( $this->config->get( 'RealnamesBareStyleBlankName' ) ) {
				$styleBlankName = $this->config->get( 'RealnamesBareStyleBlankName' );
			}
			if ( $this->config->get( 'RealnamesBareStyleSameName' ) ) {
				$styleSameName = $this->config->get( 'RealnamesBareStyleSameName' );
			}
			$m['linkstart'] = '';
			$m['linkend'] = '';
		}

		if ( !$style ) {
			// error
			self::debug( __METHOD__, 'error, blank style configuration' );
			return $m['all'];
		}

		// get the formatting code
		$format = $this->config->get( 'RealnamesStyles' )[$style];

		// we have a blank realname, and the admin doesn't want to see them,
		// or his chosen format will not display a username at all
		if ( $m['realname'] === '' && (
			!$this->config->get( 'RealnamesBlank' ) || strpos( $format, '$2' ) === false
		) ) {
			$format = $this->config->get( 'RealnamesStyles' )[$styleBlankName];
		}

		if ( $this->config->get( 'RealnamesSmart' )
			&& $this->config->get( 'RealnamesSmart' )['same']
			&& $m['username'] === $m['realname']
			&& strpos( $format, '$2' ) !== false
			&& strpos( $format, '$3' ) !== false
		) {
			// we only do this if both username and realname will be displayed in
			// the user's format
			self::debug( __METHOD__, 'smart dupe detected' );

			// we're going to display: John - John
			// this is silly. The smart thing to do
			// is infact nothing (in the name)
			$format = $this->config->get( 'RealnamesStyles' )[$styleSameName];
		}

		// plug in our values to the format desired
		// redo to ensure order
		$text = wfMsgReplaceArgs( $format, [
			$m['linkstart'],
			str_replace( '_', ' ', $m['username'] ),
			str_replace( '_', ' ', $m['realname'] ),
			$m['linkend'],
		] );

		self::debug( __METHOD__, 'replacing with ' . print_r( $text, true ) );

		return $text;
	}

	/**
	 * gather list of namespace prefixes in the wiki's language.
	 * this is a regex string.
	 *
	 * @param bool $encode
	 *
	 * @return string regex namespace options
	 *
	 * @since 2011-09-22, 0.2
	 */
	public function getNamespacePrefixes( bool $encode = false ): string {
		if ( $encode ) {
			$prefixes = self::$namespacePrefixesEncoded;
		} else {
			$prefixes = self::$namespacePrefixes;
		}

		// if we already figured it all out, just use that again
		if ( $prefixes !== null ) {
			return $prefixes;
		}

		// always catch this one
		$namespaces = [
			'User',
			'User_talk',
			'User talk',
		];

		// add in user specified ones
		$namespaces = array_merge( $namespaces, array_values( $this->config->get( 'RealnamesNamespaces' ) ) );

		// user namespace's primary name in the wiki lang
		$namespaces[] = $this->lang->getNsText( NS_USER );
		$namespaces[] = $this->lang->getNsText( NS_USER_TALK );

		$nss = $this->lang->getNamespaceAliases();

		foreach ( $nss as $name => $space ) {
			if ( in_array( $space, [ NS_USER, NS_USER_TALK ] ) ) {
				$namespaces[] = $name;
			}
		}

		// clean up
		$namespaces = array_unique( $namespaces );

		if ( $encode ) {
			$namespaces = array_map( 'urlencode', $namespaces );
		}

		// Escape namespaces for use in regex delimited with '/'.
		// Shouldn't do much for most namespaces.
		$namespaces = array_map(
			static function ( $namespace ) {
				return preg_quote( $namespace, '/' );
			},
			$namespaces
		);

		$prefixes = '(?:(?:' . implode( '|', $namespaces ) . '):)';

		self::debug( __METHOD__, 'namespace prefixes: ' . $prefixes );

		if ( $encode ) {
			self::$namespacePrefixesEncoded = $prefixes;
		} else {
			self::$namespacePrefixes = $prefixes;
		}

		return $prefixes;
	}

	/**
	 * >= 0.1, change all usernames to realnames.
	 *
	 * @param OutputPage $out The OutputPage object.
	 * @param Skin $skin object that will be used to generate the page, added in 1.13.
	 *
	 * @return void
	 *
	 * @since 2011-09-16, 0.1
	 * @note  OutputPageBeforeHTML does not work for Special pages like RecentChanges or ActiveUsers
	 * @note  requires MediaWiki 1.7.0
	 * @see   hook documentation https://www.mediawiki.org/wiki/Manual:Hooks/BeforePageDisplay
	 */
	public function onBeforePageDisplay( $out, $skin ): void {
		$title = $out->getTitle();

		if ( $this->config->get( 'RealnamesReplacements' )['title'] ) {
			// article title
			self::debug( __METHOD__, 'searching article title...' );

			// special user page handling
			// User:
			if ( in_array( $title->getNamespace(), [ NS_USER, NS_USER_TALK ] ) ) {
				// swap out the specific username from title
				// this overcomes the problem lookForBare has with spaces and underscores in names
				$reg = '/'
					. $this->getNamespacePrefixes()
					. '\s*('
					. preg_quote( $title->getText(), '/' )
					. ')(?:\/.+)?/';
				$bare = $this->lookForBare(
					$out->getPageTitle(),
					$reg
				);
				$out->setPagetitle( $bare );
			}

			// this should also affect the html head title
			$out->setPageTitle( $this->lookForBare( $out->getPageTitle() ) );
		}

		if ( $this->config->get( 'RealnamesReplacements' )['subtitle'] ) {
			// subtitle (say, on revision pages)
			self::debug( __METHOD__, 'searching article subtitle...' );
			$out->setSubtitle( $this->lookForLinks( $out->getSubtitle() ) );
		}

		if ( $this->config->get( 'RealnamesReplacements' )['body'] ) {
			// article html text
			self::debug( __METHOD__, 'searching article body...' );
			$out->mBodytext = $this->lookForLinks( $out->getHTML() );
		}
	}

	/**
	 * @param User $user
	 * @param array &$userPageOpt
	 */
	private function transformUsernameToRealname( User $user, array &$userPageOpt ): void {
		// replace the name of the logged-in user
		if ( isset( $userPageOpt )
			&& isset( $userPageOpt['text'] ) ) {
			// fake the match, we know it's there
			$m = [
				'all' => $userPageOpt['text'],
				'username' => $userPageOpt['text'],
				'realname' => $user->getRealName(),
			];
			$userPageOpt['text'] = $this->replace( $m );
		}
	}

	/**
	 * >= 0.2, change all usernames to realnames in url bar.
	 * change all usernames to realnames in skin top right links bar
	 *
	 * @param Skin $skin
	 * @param array &$links
	 * @phpcs:disable MediaWiki.NamingConventions.LowerCamelFunctionsName.FunctionName
	 *
	 * @return void
	 *
	 * @since 2011-09-22, 0.2
	 * @see   hook documentation https://www.mediawiki.org/wiki/Manual:Hooks/SkinTemplateNavigation::Universal
	 * @note  does nothing for Timeless skin
	 */
	public function onSkinTemplateNavigation__Universal( $skin, &$links ): void {
		// phpcs:enable MediaWiki.NamingConventions.LowerCamelFunctionsName.FunctionName
		// using // phpcs:ignore after docblock doesn't work, it shows
		// MediaWiki.Commenting.FunctionComment.MissingDocumentationPublic
		if ( $this->config->get( 'RealnamesReplacements' )['personnal'] ) {
			self::debug( __METHOD__, 'searching personnal urls...' );
			// We check isset here, because the mere act of passing this
			// will cause it to be set to null, which will affect output
			// for logged out users.
			if ( isset( $links['user-page']['userpage'] ) ) {
				$this->transformUsernameToRealname( $skin->getUser(), $links['user-page']['userpage'] );
			}
			if ( isset( $links['user-menu']['userpage'] ) ) {
				$this->transformUsernameToRealname( $skin->getUser(), $links['user-menu']['userpage'] );
			}
		}
	}

	/**
	 * scan and replace plain usernames of the form User:username into real names.
	 *
	 * @param string $text to scan
	 * @param string|null $pattern to match, null for default
	 *
	 * @return string with realnames replaced in
	 *
	 * @since 2011-09-16, 0.1
	 * @note  bug: we have problems with users with underscores (they become spaces) or spaces,
	 *    we tend to just strip the User: and leave the username, but we only modify the
	 *    first word so some weird style might screw it up (2011-09-17, ofb)
	 */
	protected function lookForBare( string $text, ?string $pattern = null ): string {
		// considered doing [^<]+ here to catch names with spaces or underscores,
		// which works for most titles but is not universal
		$pattern ??= '/' . $this->getNamespacePrefixes() . '([^ \t]+)(\/.+)?/';

		self::debug( __METHOD__, 'pattern: ' . $pattern );
		// create_function is slow
		$ret = preg_replace_callback(
			$pattern,
			[
				$this,
				'checkBare',
			],
			$text
		);

		return $ret;
	}

	/**
	 * scan and replace username links into realname links.
	 *
	 * @param string $text to scan
	 * @param string|null $pattern to match, null for default
	 *
	 * @return string with realnames replaced in
	 *
	 * @since 2011-09-16, 0.1
	 */
	protected function lookForLinks( string $text, ?string $pattern = null ): string {
		self::debug( __METHOD__, 'before: ' . $this->getNamespacePrefixes( false ) );
		self::debug( __METHOD__, 'after: ' . $this->getNamespacePrefixes( true ) );
		$pattern ??= '/(<a\b[^">]+href="[^">]+'
			. $this->getNamespacePrefixes( true )
			. '([^"\\?\\&>]+)[^>]+>(?:\s*<bdi>)?)'
			. $this->getNamespacePrefixes()
			. '?([^>]+)((?:<\\/bdi>)?<\\/a>)/';

		// create_function is slow
		$preg = preg_replace_callback(
			$pattern,
			[
				$this,
				'checkLink',
			],
			$text
		);
		return $preg;
	}

	/**
	 * obtains user information based on a match for future replacement.
	 *
	 * @param array $m keyed with strings called
	 *    \li<em>linkstart</em> (optional)
	 *    \li<em>username</em>
	 *    \li<em>realname</em> (optional)
	 *    \li<em>linkend</em> (optional)
	 *
	 * @return \string formatted text to replace the match with
	 *
	 * @since 2011-09-16, 0.1
	 */
	protected function replace( array $m ): string {
		$debug_msg = 'matched '
			. ( $m['username'] ?? print_r( $m, true ) );
		self::debug( __METHOD__, $debug_msg );

		if ( !isset( self::$realnames[$m['username']] ) ) {
			// we don't have it cached
			if ( isset( $m['realname'] ) ) {
				// we got it elsewhere
				$realname = $m['realname'];
			} else {
				// time to do a lookup
				$user = $this->userFactory->newFromName( $m['username'] );

				if ( !$user ) {
					self::debug( __METHOD__, 'skipped, invalid user: ' . $m['username'] );
					return $m['all'];
				}

				$realname = $user->getRealname();
			}

			self::$realnames[$m['username']] = htmlspecialchars( trim( $realname ) );
		}

		// this may be blank
		$m['realname'] = self::$realnames[$m['username']];

		return $this->display( $m );
	}
}
