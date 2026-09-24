<?php
/**
 * Standalone tests for the ported parsing logic.
 *
 * No PHPUnit and no WordPress required: the handful of WP functions used by
 * the pure helpers are stubbed below.
 *
 * Run on the VPS (or anywhere with PHP 7.4+):
 *     php tests/run-tests.php
 *
 * The regexes are ported from 4kbolly.py, so these expectations are the ones
 * the live tmovies importer already accepts. They are the highest-risk part of
 * the bridge: Python re syntax -> PCRE changes subtly.
 */

define( 'ABSPATH', __DIR__ . '/' );

// ---- minimal WP stubs ---------------------------------------------------

$GLOBALS['xdm_test_options'] = array(
	'bridge_base'   => '',
	'bridge_key'    => '',
	'tgmovies_base' => '',
	'wp_secret'     => '',
	'post_author'   => 'Bruno Goyal',
	'shortener'     => '', // empty => shortener is a pass-through
);

if ( ! function_exists( 'get_option' ) ) {
	function get_option( $name, $default = false ) {
		return 'xdm_bridge_settings' === $name ? $GLOBALS['xdm_test_options'] : $default;
	}
}
if ( ! function_exists( 'update_option' ) ) {
	function update_option( $name, $value ) {
		$GLOBALS['xdm_test_options'] = $value;
		return true;
	}
}
if ( ! function_exists( 'esc_url_raw' ) ) {
	function esc_url_raw( $url ) {
		$url = trim( (string) $url );
		return preg_match( '#^https?://#i', $url ) ? $url : '';
	}
}
if ( ! function_exists( 'esc_url' ) ) {
	function esc_url( $url ) {
		return htmlspecialchars( esc_url_raw( $url ), ENT_QUOTES, 'UTF-8' );
	}
}
if ( ! function_exists( 'esc_html' ) ) {
	function esc_html( $text ) {
		return htmlspecialchars( (string) $text, ENT_QUOTES, 'UTF-8' );
	}
}
if ( ! function_exists( '__' ) ) {
	function __( $text, $domain = null ) {
		return $text;
	}
}
if ( ! function_exists( '_x' ) ) {
	function _x( $text, $context, $domain = null ) {
		return $text;
	}
}
if ( ! function_exists( 'untrailingslashit' ) ) {
	function untrailingslashit( $s ) {
		return rtrim( (string) $s, '/\\' );
	}
}
if ( ! function_exists( 'wp_remote_get' ) ) {
	function wp_remote_get( $url, $args = array() ) {
		return new WP_Error( 'xdm_test_no_http', 'network disabled in tests' );
	}
}
if ( ! function_exists( 'wp_remote_post' ) ) {
	function wp_remote_post( $url, $args = array() ) {
		return new WP_Error( 'xdm_test_no_http', 'network disabled in tests' );
	}
}
if ( ! function_exists( 'wp_remote_request' ) ) {
	function wp_remote_request( $url, $args = array() ) {
		return new WP_Error( 'xdm_test_no_http', 'network disabled in tests' );
	}
}
if ( ! function_exists( 'wp_remote_retrieve_body' ) ) {
	function wp_remote_retrieve_body( $r ) {
		return '';
	}
}
if ( ! function_exists( 'wp_remote_retrieve_response_code' ) ) {
	function wp_remote_retrieve_response_code( $r ) {
		return 0;
	}
}
if ( ! function_exists( 'wp_json_encode' ) ) {
	function wp_json_encode( $data ) {
		return json_encode( $data );
	}
}
if ( ! function_exists( 'is_wp_error' ) ) {
	function is_wp_error( $thing ) {
		return $thing instanceof WP_Error;
	}
}
if ( ! class_exists( 'WP_Error' ) ) {
	class WP_Error {
		private $code;
		private $message;
		public function __construct( $code = '', $message = '' ) {
			$this->code    = $code;
			$this->message = $message;
		}
		public function get_error_message() {
			return $this->message;
		}
		public function get_error_code() {
			return $this->code;
		}
	}
}

// ---- load the plugin classes under test --------------------------------

require_once __DIR__ . '/../includes/class-xdm-regex.php';
require_once __DIR__ . '/../includes/class-xdm-settings.php';
require_once __DIR__ . '/../includes/class-xdm-shortener.php';
require_once __DIR__ . '/../includes/class-xdm-writer.php';

// ---- tiny harness -------------------------------------------------------

$GLOBALS['xdm_pass'] = 0;
$GLOBALS['xdm_fail'] = 0;

function ok( $name, $actual, $expected ) {
	if ( $actual === $expected ) {
		$GLOBALS['xdm_pass']++;
		echo "  PASS  $name\n";
	} else {
		$GLOBALS['xdm_fail']++;
		echo "  FAIL  $name\n";
		echo "        expected: " . var_export( $expected, true ) . "\n";
		echo "        actual:   " . var_export( $actual, true ) . "\n";
	}
}

function section( $title ) {
	echo "\n$title\n";
}

// ---- quality ------------------------------------------------------------

section( 'quality' );
ok( '2160p', XDM_Regex::quality( 'Baby.Do.Die.Do.2026.2160p.NF.WEB-DL' ), '2160p' );
ok( '1080p', XDM_Regex::quality( 'Kalki.1080p.Hindi' ), '1080p' );
ok( '4K word boundary', XDM_Regex::quality( '4KHDHub.Sample.mkv' ), 'Unknown Quality' );
ok( '4K real', XDM_Regex::quality( 'Movie.2024.4K.WEB-DL' ), '4K' );
ok( 'empty', XDM_Regex::quality( '' ), 'Unknown Quality' );

// ---- languages ----------------------------------------------------------

section( 'languages' );
ok( 'multi', XDM_Regex::languages( 'Hindi, English, Tamil' ), 'Hindi, English, Tamil' );
ok( 'dual glued in filename', XDM_Regex::languages( 'The.Purge.Anarchy.DUAL.1080p' ), 'Dual Audio' );
ok( 'specific beats dual', XDM_Regex::languages( 'Hindi English DUAL' ), 'Hindi, English' );
ok( 'no false dual in title', XDM_Regex::languages( 'Dual (2022)' ), 'Unknown Audio' );
ok( 'none', XDM_Regex::languages( 'It.2017.1080p.BluRay' ), 'Unknown Audio' );

// ---- source tags --------------------------------------------------------

section( 'source tags' );
ok(
	'amzn webdl ddp h264',
	XDM_Regex::source_tags( 'Twilight.of.the.Warriors.Walled.In.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-PrimeFix.mkv' ),
	'AMZN WEB-DL DDP5.1 H.264'
);
ok(
	'spaced ddp joined',
	XDM_Regex::source_tags( 'The Purge 2013 1080p BluRay Hindi DDP 5.1 English DTS 5.1 x264' ),
	'BLURAY DDP5.1 DTS5.1 X264'
);
ok( 'itunes tag', XDM_Regex::source_tags( 'Show.S01E01.1080p.iP.WEB-DL' ), 'IT WEB-DL' );
ok( 'no english-word false it', XDM_Regex::source_tags( 'It.2017.1080p.BluRay' ), 'BLURAY' );
ok( 'release group not leaked', XDM_Regex::source_tags( 'Movie.2024.1080p.WEB-DL.x265-XDMovies.com' ), 'WEB-DL H.265' );

// ---- host ---------------------------------------------------------------

section( 'host' );
ok( 'hubcloud', XDM_Regex::host( 'https://hubcloud.ist/drive/abc' ), 'HubCloud' );
ok( 'hubdrive', XDM_Regex::host( 'https://hubdrive.click/x' ), 'HubDrive' );
ok( 'gdrive', XDM_Regex::host( 'https://lh3.googleusercontent.com/x' ), 'GDrive' );
ok( 'unknown', XDM_Regex::host( 'https://example.com/file' ), 'Direct' );

// ---- size / label / url -------------------------------------------------

section( 'size + label + url' );
ok( 'size', XDM_Regex::size( '1.21 GB' ), '1.21 GB' );
ok( 'size from sentence', XDM_Regex::size( 'Size: 13.02 gb' ), '13.02 GB' );
ok( 'no size', XDM_Regex::size( 'unknown' ), '' );
ok( 'label', XDM_Regex::row_label( '1080p', 'Hindi, Tamil', 'HubCloud' ), '1080P HINDI, TAMIL [HUBCLOUD]' );
ok( 'label with tags', XDM_Regex::row_label( '2160p', 'NF WEB-DL DDP5.1 H.265 Hindi', 'HubCloud' ), '2160P NF WEB-DL DDP5.1 H.265 HINDI [HUBCLOUD]' );
ok( 'label no lang', XDM_Regex::row_label( '720p', '', 'GDrive' ), '720P [GDRIVE]' );
ok( 'safe url ok', XDM_Regex::safe_url( 'https://hubcloud.ist/drive/a' ), 'https://hubcloud.ist/drive/a' );
ok( 'safe url rejects js', XDM_Regex::safe_url( 'javascript:alert(1)' ), '' );

// ---- movie name / language ---------------------------------------------

section( 'movie name + language' );
ok( 'tmdb preferred', XDM_Writer::movie_name( array( 'tmdb_id' => '12345', 'title' => 'Kalki' ) ), '12345' );
ok( 'title fallback', XDM_Writer::movie_name( array( 'tmdb_id' => null, 'title' => 'Kalki' ) ), 'Kalki' );
ok( 'language passthrough', XDM_Writer::language( array( 'language' => 'Hindi, Tamil' ) ), 'Hindi, Tamil' );
ok( 'language sentinel', XDM_Writer::language( array( 'language' => '' ) ), 'Unknown Audio' );

// ---- build_rows: the contract the importer parses ----------------------

section( 'build_rows (importer contract)' );
$result = array(
	'title'    => 'Baby Do Die Do',
	'tmdb_id'  => '1512084',
	'language' => 'Hindi, Tamil',
	'cards'    => array(
		array(
			'raw_title' => 'Baby.Do.Die.Do.2026.2160p.NF.WEB-DL.DDP5.1.Atmos.H.265-XDMovies.com.mkv',
			'size'      => '13.02 GB',
			'language'  => '',
			'url'       => 'https://hubcloud.ist/drive/aaa',
			'status'    => 'resolved',
		),
		array(
			'raw_title' => 'Baby.Do.Die.Do.2026.1080p.NF.WEB-DL.DDP5.1.H.264-XDMovies.com.mkv',
			'size'      => '4.90 GB',
			'language'  => 'Hindi, Tamil',
			'url'       => 'https://hubdrive.click/bbb',
			'status'    => 'resolved',
		),
		array(
			'raw_title' => 'failed card',
			'size'      => '',
			'language'  => '',
			'url'       => '', // unresolved -> must be skipped
			'status'    => 'failed',
		),
	),
);
$rows = XDM_Writer::build_rows( $result );
ok( 'build_rows is a string', is_string( $rows ), true );
$expected = "<b>2160P</b>\n"
	. "2160P NF WEB-DL DDP5.1 ATMOS H.265 HINDI, TAMIL [HUBCLOUD]|https://hubcloud.ist/drive/aaa|13.02 GB\n"
	. "<b>1080P</b>\n"
	. "1080P NF WEB-DL DDP5.1 H.264 HINDI, TAMIL [HUBDRIVE]|https://hubdrive.click/bbb|4.90 GB";
ok( 'exact importer payload', $rows, $expected );

section( 'build_rows edge cases' );
ok( 'no cards -> WP_Error', is_wp_error( XDM_Writer::build_rows( array( 'cards' => array() ) ) ), true );
$all_failed = XDM_Writer::build_rows( array( 'cards' => array( array( 'url' => '', 'raw_title' => 'x' ) ) ) );
ok( 'all unresolved -> WP_Error', is_wp_error( $all_failed ), true );

// ---- report -------------------------------------------------------------

echo "\n";
echo "{$GLOBALS['xdm_pass']} passed, {$GLOBALS['xdm_fail']} failed\n";
exit( $GLOBALS['xdm_fail'] > 0 ? 1 : 0 );
