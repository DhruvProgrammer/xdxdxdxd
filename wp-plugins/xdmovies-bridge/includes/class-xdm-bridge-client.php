<?php
/**
 * HTTP client for the Render bridge (app.py).
 *
 * The WordPress plugin never talks to the VPS bot directly: the bridge holds
 * the bot API key so this file never sees it, and it gives us a stable TLS
 * endpoint that survives a VPS IP change.
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Thin wp_remote_* wrapper around the bridge endpoints.
 */
class XDM_Bridge_Client {

	/**
	 * Perform a bridge request.
	 *
	 * @param string $method  HTTP method.
	 * @param string $path    Path beginning with "/" (e.g. /xdm-result/<id>).
	 * @param array  $body    Optional JSON body.
	 * @return array|WP_Error Decoded response on 2xx, WP_Error otherwise.
	 */
	public static function request( $method, $path, $body = null ) {
		$base = XDM_Settings::bridge_base();
		if ( '' === $base ) {
			return new WP_Error( 'xdm_no_bridge', __( 'Bridge URL is not configured.', 'xdmovies-bridge' ) );
		}
		$key = XDM_Settings::bridge_key();
		if ( '' === $key ) {
			return new WP_Error( 'xdm_no_key', __( 'Bridge key is not configured.', 'xdmovies-bridge' ) );
		}

		$args = array(
			'method'  => strtoupper( $method ),
			'timeout' => 20,
			'headers' => array(
				'X-Bridge-Key' => $key,
				'Accept'       => 'application/json',
			),
		);
		if ( null !== $body ) {
			$args['headers']['Content-Type'] = 'application/json';
			$args['body']                    = wp_json_encode( $body );
		}

		$response = wp_remote_request( untrailingslashit( $base ) . $path, $args );
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		$raw  = (string) wp_remote_retrieve_body( $response );
		$data = json_decode( $raw, true );
		if ( ! is_array( $data ) ) {
			$data = array( 'error' => 'invalid_json' );
		}

		// The bridge relays the bot's status code, so a 202 on create and a
		// 401 from a stale key both arrive here verbatim.
		if ( $code < 200 || $code >= 300 ) {
			$message = isset( $data['error'] ) ? (string) $data['error'] : ( 'http_' . $code );
			return new WP_Error( 'xdm_bridge_' . $code, $message, array( 'status' => $code, 'body' => $data ) );
		}
		return $data;
	}

	/**
	 * Ask the bridge to start a resolve job.
	 *
	 * @param string $xdmovies_url Canonical xdmovies detail URL.
	 * @param string $card_filter  all|episodes|packs.
	 * @param bool   $force        Skip the bot's dedup window.
	 * @return array|WP_Error
	 */
	public static function create_job( $xdmovies_url, $card_filter = 'all', $force = false ) {
		return self::request(
			'POST',
			'/xdm-scrape',
			array(
				'url'         => $xdmovies_url,
				'card_filter' => $card_filter,
				'force'       => (bool) $force,
			)
		);
	}

	/**
	 * Poll a job.
	 *
	 * @param string $job_id Job id from create_job.
	 * @return array|WP_Error
	 */
	public static function get_job( $job_id ) {
		return self::request( 'GET', '/xdm-result/' . rawurlencode( $job_id ) );
	}

	/**
	 * Cancel a job.
	 *
	 * @param string $job_id Job id.
	 * @return array|WP_Error
	 */
	public static function cancel_job( $job_id ) {
		return self::request( 'DELETE', '/xdm-cancel/' . rawurlencode( $job_id ) );
	}
}
