<?php
/**
 * End-to-end contract test.
 *
 * Runs the REAL plugin classes (XDM_Bridge_Client, XDM_Writer,
 * XDM_Shortener) against the REAL bridge relay over real HTTP, with stub
 * servers standing in for the bot API and the tmovies importer.
 *
 * The WordPress HTTP functions here are backed by cURL, so the plugin's own
 * request construction, header handling, status-code checks and JSON parsing
 * all execute for real. Only the WordPress *bookkeeping* (post types, cron,
 * nonces) is stubbed, because that is framework glue the plugin does not own.
 *
 * Usage:
 *   BRIDGE_URL=http://127.0.0.1:8091 \
 *   BRIDGE_KEY=plugin-shared-key \
 *   TGMOVIES_URL=http://127.0.0.1:8092 \
 *   php tests/e2e/integration.php
 */

define( 'ABSPATH', __DIR__ . '/' );

$bridge_url  = getenv( 'BRIDGE_URL' ) ?: '';
$bridge_key  = getenv( 'BRIDGE_KEY' ) ?: '';
$tgmovies    = getenv( 'TGMOVIES_URL' ) ?: '';
// The relay's outbound key, i.e. what the bot's XDMOVIES_API_KEY is set to.
// Used only to assert the relay never leaks it in a response body.
define( 'BOT_KEY', getenv( 'STUB_BOT_KEY' ) ?: 'bot-secret-key' );

if ( '' === $bridge_url || '' === $bridge_key || '' === $tgmovies ) {
	fwrite( STDERR, "BRIDGE_URL, BRIDGE_KEY and TGMOVIES_URL are required\n" );
	exit( 2 );
}

// ---- WordPress stubs, cURL-backed --------------------------------------

$GLOBALS['xdm_options'] = array(
	'bridge_base'   => rtrim( $bridge_url, '/' ),
	'bridge_key'    => $bridge_key,
	'tgmovies_base' => rtrim( $tgmovies, '/' ),
	'wp_secret'     => 'wp-secret-abc',
	'post_author'   => 'Bruno Goyal',
	'shortener'     => '', // shortener is not under test here
);

function get_option( $name, $default = false ) {
	return 'xdm_bridge_settings' === $name ? $GLOBALS['xdm_options'] : $default;
}
function update_option( $name, $value ) {
	$GLOBALS['xdm_options'] = $value;
	return true;
}

/**
 * Minimal cURL implementation of wp_remote_request.
 */
function wp_remote_request( $url, $args = array() ) {
	$method  = isset( $args['method'] ) ? strtoupper( $args['method'] ) : 'GET';
	$headers = isset( $args['headers'] ) ? $args['headers'] : array();
	$timeout = isset( $args['timeout'] ) ? (int) $args['timeout'] : 20;

	$ch = curl_init();
	curl_setopt( $ch, CURLOPT_URL, $url );
	curl_setopt( $ch, CURLOPT_RETURNTRANSFER, true );
	curl_setopt( $ch, CURLOPT_CUSTOMREQUEST, $method );
	curl_setopt( $ch, CURLOPT_TIMEOUT, $timeout );
	curl_setopt( $ch, CURLOPT_HTTPHEADER, array_map(
		static function ( $k, $v ) { return $k . ': ' . $v; },
		array_keys( $headers ), array_values( $headers )
	) );
	if ( isset( $args['body'] ) ) {
		curl_setopt( $ch, CURLOPT_POSTFIELDS, $args['body'] );
	}
	$body   = curl_exec( $ch );
	$errno  = curl_errno( $ch );
	$status = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
	curl_close( $ch );

	if ( $errno ) {
		return new WP_Error( 'http_request_failed', 'curl error ' . $errno );
	}
	return array( 'body' => $body, 'status' => $status );
}

function wp_remote_get( $url, $args = array() ) {
	$args['method'] = 'GET';
	return wp_remote_request( $url, $args );
}
function wp_remote_post( $url, $args = array() ) {
	$args['method'] = 'POST';
	return wp_remote_request( $url, $args );
}
function wp_remote_retrieve_body( $r ) {
	return is_array( $r ) ? $r['body'] : '';
}
function wp_remote_retrieve_response_code( $r ) {
	return is_array( $r ) ? $r['status'] : 0;
}
function esc_url_raw( $url ) {
	$url = trim( (string) $url );
	return preg_match( '#^https?://#i', $url ) ? $url : '';
}
function esc_url( $url ) {
	return htmlspecialchars( esc_url_raw( $url ), ENT_QUOTES, 'UTF-8' );
}
function esc_html( $t ) {
	return htmlspecialchars( (string) $t, ENT_QUOTES, 'UTF-8' );
}
function untrailingslashit( $s ) {
	return rtrim( (string) $s, '/\\' );
}
function wp_json_encode( $d ) {
	return json_encode( $d );
}
function is_wp_error( $t ) {
	return $t instanceof WP_Error;
}
if ( ! class_exists( 'WP_Error' ) ) {
	class WP_Error {
		private $code;
		private $message;
		public function __construct( $c = '', $m = '' ) {
			$this->code = $c;
			$this->message = $m;
		}
		public function get_error_message() {
			return $this->message;
		}
		public function get_error_code() {
			return $this->code;
		}
	}
}
if ( ! function_exists( '__' ) ) {
	function __( $t, $d = null ) {
		return $t;
	}
}

// ---- load the REAL plugin classes --------------------------------------

require_once __DIR__ . '/../../wp-plugins/xdmovies-bridge/includes/class-xdm-regex.php';
require_once __DIR__ . '/../../wp-plugins/xdmovies-bridge/includes/class-xdm-settings.php';
require_once __DIR__ . '/../../wp-plugins/xdmovies-bridge/includes/class-xdm-shortener.php';
require_once __DIR__ . '/../../wp-plugins/xdmovies-bridge/includes/class-xdm-bridge-client.php';
require_once __DIR__ . '/../../wp-plugins/xdmovies-bridge/includes/class-xdm-writer.php';

// ---- harness ------------------------------------------------------------

$pass = 0;
$fail = 0;

function ok( $name, $actual, $expected ) {
	global $pass, $fail;
	if ( $actual === $expected ) {
		$pass++;
		echo "  PASS  $name\n";
	} else {
		$fail++;
		echo "  FAIL  $name\n";
		echo "        expected: " . var_export( $expected, true ) . "\n";
		echo "        actual:   " . var_export( $actual, true ) . "\n";
	}
}

function section( $t ) {
	echo "\n$t\n";
}

// ---- 1. health ----------------------------------------------------------

section( 'relay health' );
$health = XDM_Bridge_Client::request( 'GET', '/health' );
// health is unauthenticated at the relay only via the public route, so call
// it through a raw curl to prove it answers without the key.
$raw = wp_remote_request( rtrim( $GLOBALS['xdm_options']['bridge_base'], '/' ) . '/health' );
$raw_json = json_decode( wp_remote_retrieve_body( $raw ), true );
ok( 'health 200', wp_remote_retrieve_response_code( $raw ), 200 );
ok( 'health ok', isset( $raw_json['ok'] ) && true === $raw_json['ok'], true );
// The relay must report whether keys are set, never their values.
ok( 'health reports bot key present', isset( $raw_json['bot_key_set'] ) && true === $raw_json['bot_key_set'], true );
ok( 'health leaks no key material', false === strpos( wp_remote_retrieve_body( $raw ), $GLOBALS['xdm_options']['bridge_key'] ), true );
ok( 'health leaks no bot key', false === strpos( wp_remote_retrieve_body( $raw ), BOT_KEY ), true );

// ---- 2. auth -----------------------------------------------------------

section( 'relay auth' );
// Good key must work.
$authed = XDM_Bridge_Client::request( 'GET', '/xdm-result/e2e-job-0001' );
ok( 'correct key accepted', is_wp_error( $authed ), false );

// A wrong key must be refused by the relay.
$good = $GLOBALS['xdm_options']['bridge_key'];
$GLOBALS['xdm_options']['bridge_key'] = 'totally-wrong-key';
$bad = XDM_Bridge_Client::get_job( 'e2e-job-0001' );
ok( 'wrong bridge key rejected', is_wp_error( $bad ), true );
$GLOBALS['xdm_options']['bridge_key'] = $good;

// ---- 3. create job (plugin -> relay -> bot) ----------------------------

section( 'create job through the relay' );
$created = XDM_Bridge_Client::create_job( 'https://top.xdmovies.wtf/movies/baby-1512084', 'all' );
ok( 'create is not an error', is_wp_error( $created ), false );
ok( 'job id returned', isset( $created['job_id'] ) ? $created['job_id'] : '', 'e2e-job-0001' );
ok( 'status queued', isset( $created['status'] ) ? $created['status'] : '', 'queued' );

$bad_url = XDM_Bridge_Client::create_job( 'https://evil.example/movies/x', 'all' );
ok( 'non-xdmovies url rejected by bot', is_wp_error( $bad_url ), true );

// ---- 4. poll result ----------------------------------------------------

section( 'poll result through the relay' );
$polled = XDM_Bridge_Client::get_job( 'e2e-job-0001' );
ok( 'poll not an error', is_wp_error( $polled ), false );
ok( 'status done', isset( $polled['status'] ) ? $polled['status'] : '', 'done' );
ok( 'result present', ! empty( $polled['result']['cards'] ), true );
ok( 'tmdb present', isset( $polled['result']['tmdb_id'] ) ? $polled['result']['tmdb_id'] : '', '1512084' );

// ---- 5. cancel ---------------------------------------------------------

section( 'cancel through the relay' );
$cancelled = XDM_Bridge_Client::cancel_job( 'e2e-job-0001' );
ok( 'cancel not an error', is_wp_error( $cancelled ), false );
ok( 'cancel status', isset( $cancelled['status'] ) ? $cancelled['status'] : '', 'cancelling' );

// ---- 6. needs_human passthrough ----------------------------------------

section( 'needs_human passthrough' );
$parked = XDM_Bridge_Client::get_job( 'e2e-needs-human' );
ok( 'parked status', isset( $parked['status'] ) ? $parked['status'] : '', 'needs_human' );
ok( 'solve url preserved', isset( $parked['solve_url'] ) ? $parked['solve_url'] : '', 'http://vps:8080/solve/abc123' );

// ---- 7. build + publish (plugin -> tmovies) ----------------------------

section( 'build rows and publish' );
$rows = XDM_Writer::build_rows( $polled['result'] );
ok( 'rows built', is_string( $rows ) && false !== strpos( $rows, '<b>2160P</b>' ), true );
ok( 'row count', substr_count( $rows, "\n" ) + 1, 4 );

$published = XDM_Writer::publish( $polled['result'] );
ok( 'publish not an error', is_wp_error( $published ), false );
ok( 'publish mode', isset( $published['mode'] ) ? $published['mode'] : '', 'auto-post' );
ok( 'publish ok flag', isset( $published['ok'] ) ? $published['ok'] : null, true );
ok( 'publish post_url', isset( $published['post_url'] ) ? $published['post_url'] : '', 'https://tgmovies.test/movies/baby-die-do/' );

// ---- report ------------------------------------------------------------

echo "\n{$pass} passed, {$fail} failed\n";
exit( $fail > 0 ? 1 : 0 );
