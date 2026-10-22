<?php
/**
 * Cologne Blue: A nicer-looking alternative to Standard.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
 * http://www.gnu.org/copyleft/gpl.html
 *
 * @file
 */

namespace MediaWiki\Extension\CologneBlue;

// phpcs:disable MediaWiki.NamingConventions.LowerCamelFunctionsName.FunctionName

use MediaWiki\Hook\SkinTemplateNavigation__UniversalHook;
use MediaWiki\Skin\SkinComponentUtils;
use MediaWiki\SpecialPage\SpecialPage;
use MediaWiki\Title\Title;
use Skin;
use SkinTemplate;

/**
 * @ingroup Skins
 */
class Hooks implements SkinTemplateNavigation__UniversalHook {
	/**
	 * Construct menu for the Cologne Blue footer
	 * from the existing data given to the
	 * SkinTemplateNavigationUniversal hook
	 * @param Skin $skin
	 * @param array $content_nav as constructed in SkinTemplateNavigationUniversal hook
	 * @return array
	 */
	private static function getFooterShortcuts( $skin, $content_nav ) {
		$footerShortcuts = [];
		$actions = $content_nav['actions'];
		$namespaces = $content_nav['namespaces'];
		$prefix = 'cb-';

		foreach ( [ 'edit', 'viewsource' ] as $key ) {
			if ( isset( $actions[$key] ) ) {
				$footerShortcuts[$prefix . $key] = array_merge( [], $actions[$key] );
			}
		}

		foreach ( [ 'watch', 'unwatch', 'talk', 'history' ] as $key ) {
			if ( isset( $namespaces[$key] ) ) {
				$footerShortcuts[$prefix . $key] = array_merge( [], $namespaces[$key], [
					// disable ID construction
					'context' => null,
				] );
			}
		}

		$out = $skin->getOutput();
		$title = $out->getTitle();
		$thispage = $title->getPrefixedDBkey();

		if ( $out->isArticleRelated() ) {
			$footerShortcuts['cb-whatlinkshere'] = [
				'href' => SpecialPage::getTitleFor( 'Whatlinkshere', $thispage )->getLocalURL(),
				'text' => $skin->msg( 'whatlinkshere' )->text(),
				'title' => $skin->msg( "tooltip-t-whatlinkshere" )->escaped(),
			];
			$footerShortcuts['cb-info'] = [
				'text' => $skin->msg( 'pageinfo-toolboxlink' )->text(),
				'title' => $skin->msg( "tooltip-info" )->escaped(),
				'href' => $title->getLocalURL( "action=info" ),
			];

			if ( $title->exists() || $title->inNamespace( NS_CATEGORY ) ) {
				$footerShortcuts['recentchangeslinked'] = [
					'href' => SpecialPage::getTitleFor( 'Recentchangeslinked',
						$title->getPrefixedDBkey()
					)->getLocalURL(),
				];
			}
		}

		$user = $skin->getRelevantUser();
		if ( $user ) {
			$rootUser = $user->getName();
			if ( $skin->showEmailUser( $user ) ) {
				// $this->msg( 'tool-link-emailuser', $rootUser )->text()
				$footerShortcuts['emailuser'] = [
					'text' => $skin->msg( 'tool-link-emailuser', $rootUser )->text(),
					'href' => SkinComponentUtils::makeSpecialUrlSubpage( 'Emailuser', $rootUser ),
				];
			}
		}

		return $footerShortcuts;
	}

	/**
	 * Construct menu for the Cologne Blue footer
	 * for privileged users from the existing data given to the
	 * SkinTemplateNavigationUniversal hook
	 * @param Skin $skin
	 * @param array $content_nav as constructed in SkinTemplateNavigationUniversal hook
	 * @return array
	 */
	private static function getFooterShortcutsPrivilegedUsers( $skin, $content_nav ) {
		$element = [];
		$keys = [ 'delete', 'undelete', 'protect', 'unprotect', 'move' ];
		foreach ( $keys as $key ) {
			if ( isset( $content_nav['actions'][$key] ) ) {
				$element['cb-' . $key] = array_merge( [], $content_nav['actions'][$key], [
					'title' => $skin->msg( "tooltip-ca-$key" )->escaped(),
				] );
			}
		}
		return $element;
	}

	/**
	 * Adds CologneBlue-specific items to the sidebar: qbedit, qbpageoptions and qbmyoptions menus.
	 *
	 * @param SkinTemplate $skin
	 * @param array &$content_navigation
	 */
	public function onSkinTemplateNavigation__Universal( $skin, &$content_navigation ): void {
		if ( $skin->getSkinName() !== 'cologneblue' ) {
			return;
		}
		// "This page" and "Edit" menus
		// We need to do some massaging here... we reuse all of the items,
		// except for $...['views']['view'], as $...['namespaces']['main'] and
		// $...['namespaces']['talk'] together serve the same purpose. We also
		// don't use $...['variants'], these are displayed in the top menu.
		$qbpageoptions = $content_navigation['namespaces'];
		if ( isset( $content_navigation['views']['history'] ) ) {
			$qbpageoptions['history'] = $content_navigation['views']['history'];
		}
		if ( isset( $content_navigation['actions']['watch'] ) ) {
			$qbpageoptions['watch'] = $content_navigation['actions']['watch'];
		} elseif ( isset( $content_navigation['actions']['unwatch'] ) ) {
			$qbpageoptions['unwatch'] = $content_navigation['actions']['unwatch'];
		}

		unset( $content_navigation['actions']['watch'] );
		unset( $content_navigation['actions']['unwatch'] );

		$views = $content_navigation['views'] ?? [];

		$qbEditLinks = [];
		if ( isset( $views['edit'] ) ) {
			$qbEditLinks['edit'] = $views['edit'];
		}

		if ( isset( $views['addsection'] ) ) {
			$qbEditLinks['addsection'] = $views['addsection'];
		}
		$qbedit = array_merge(
			$qbEditLinks,
			$content_navigation['actions']
		);

		$content_navigation['namespaces'] = $qbpageoptions;
		$content_navigation['actions'] = $qbedit;
		$out = $skin->getOutput();
		$toolbox = $out->getProperty( 'cb-toolbox' );

		// Clone language menu for footer display
		$languages = $skin->getLanguages();
		if ( count( $languages ) > 0 ) {
			$content_navigation['cb-footer-languages'] = array_map( static function ( $lang ) {
				return array_merge( [], $lang, [
					// disable ID generation.
					'id' => false,
				] );
			}, $languages );
		}

		$content_navigation['cb-footer-shortcuts'] = self::getFooterShortcuts(
			$skin, $content_navigation
		);
		$content_navigation['cb-footer-shortcuts-privileged'] =
			self::getFooterShortcutsPrivilegedUsers( $skin, $content_navigation );

		$mainpage = [
			'text' => $skin->msg( 'mainpage' )->escaped(),
			'href' => Title::newMainPage()->getLocalURL(),
		];

		$about = [
			'text' => $skin->msg( 'about' )->escaped(),
			'href' => Skin::makeInternalOrExternalUrl(
				$skin->msg( 'aboutpage' )->inContentLanguage()->text()
			)
		];

		$syslinks = [
			'cb-mainpage' => $mainpage,
			'cb-about' => $about,
			'cb-help' => [
				'text' => $skin->msg( 'help' )->escaped(),
				'href' => Skin::makeInternalOrExternalUrl(
					$skin->msg( 'helppage' )->inContentLanguage()->text()
				),
			],
			'cb-faq' => [
				'text' => $skin->msg( 'faq' )->escaped(),
				'href' => Skin::makeInternalOrExternalUrl(
					$skin->msg( 'faqpage' )->inContentLanguage()->text()
				)
			],
		];

		if ( $skin->getUser()->isAnon() ) {
			$syslinks['cb-create-account'] = [
				'text' => $skin->msg( 'pt-createaccount' )->escaped(),
				'href' => Title::newFromText( 'Special:CreateAccount' )->getLocalURL(),
			];
			$syslinks['cb-login'] = [
				'text' => $skin->msg( 'pt-login' )->escaped(),
				'href' => Title::newFromText( 'Special:UserLogin' )->getLocalURL(),
			];
		} else {
			$syslinks['cb-logout'] = [
				'text' => $skin->msg( 'pt-userlogout' )->escaped(),
				'data-mw' => 'interface',
				'href' => Title::newFromText( 'Special:UserLogout' )->getLocalURL(),
			];
		}

		$content_navigation['cb-syslinks'] = $syslinks;
		$content_navigation['cb-footer-syslinks'] = [
			'cb-mainpage-footer' => $mainpage,
			'cb-about-footer' => $about,
		];

		// Removes logout, login and create account from the personal menus tool
		// for Cologne Blue for historic reasons.
		$qbmyoptions = $content_navigation['user-menu'] ?? [];
		foreach ( [ 'logout', 'createaccount', 'login' ] as $key ) {
			unset( $qbmyoptions[$key] );
		}
		$content_navigation['user-menu'] = $qbmyoptions;
	}
}
