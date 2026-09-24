<?php
/**
 * Plugin settings (stored in wp_options).
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Typed accessors + sanitising save for every option.
 */
class XDM_Settings {

	const OPTION = 'xdm_bridge_settings';

	/**
	 * Defaults for a fresh install.
	 *
	 * @return array
	 */
	public static function defaults() {
		return array(
			'bridge_base'   => '',
			'bridge_key'    => '',
			'tgmovies_base' => '',
			'wp_secret'     => '',
			'post_author'   => 'Bruno Goyal',
			'shortener'     => 'https://cinealert.in/?api=api_1page_2026&url=',
		);
	}

	/**
	 * Read one option.
	 *
	 * @param string $key     Option key.
	 * @param mixed  $default Fallback.
	 * @return mixed
	 */
	public static function get( $key, $default = '' ) {
		$stored = get_option( self::OPTION, array() );
		$stored = is_array( $stored ) ? $stored : array();
		return array_key_exists( $key, $stored ) ? $stored[ $key ] : $default;
	}

	/**
	 * Bridge base URL (the Render app), no trailing slash.
	 *
	 * @return string
	 */
	public static function bridge_base() {
		return untrailingslashit( trim( (string) self::get( 'bridge_base' ) ) );
	}

	/**
	 * Bridge shared key (sent as X-Bridge-Key).
	 *
	 * @return string
	 */
	public static function bridge_key() {
		return trim( (string) self::get( 'bridge_key' ) );
	}

	/**
	 * tmovies site base URL.
	 *
	 * @return string
	 */
	public static function tgmovies_base() {
		return untrailingslashit( trim( (string) self::get( 'tgmovies_base' ) ) );
	}

	/**
	 * Secret key for the tmovies REST routes.
	 *
	 * @return string
	 */
	public static function wp_secret() {
		return trim( (string) self::get( 'wp_secret' ) );
	}

	/**
	 * Author used when auto-creating a post.
	 *
	 * @return string
	 */
	public static function post_author() {
		return (string) self::get( 'post_author', 'Bruno Goyal' );
	}

	/**
	 * Shortener endpoint prefix; the encoded URL is appended.
	 *
	 * @return string
	 */
	public static function shortener_endpoint() {
		return trim( (string) self::get( 'shortener' ) );
	}

	/**
	 * Persist the settings form.
	 *
	 * @param array $input Raw $_POST values.
	 */
	public static function save( array $input ) {
		$clean = self::defaults();
		if ( isset( $input['bridge_base'] ) ) {
			$clean['bridge_base'] = esc_url_raw( trim( $input['bridge_base'] ) );
		}
		if ( isset( $input['tgmovies_base'] ) ) {
			$clean['tgmovies_base'] = esc_url_raw( trim( $input['tgmovies_base'] ) );
		}
		// Secrets are not URL fields: keep them raw but trimmed.
		foreach ( array( 'bridge_key', 'wp_secret', 'post_author', 'shortener' ) as $key ) {
			if ( isset( $input[ $key ] ) ) {
				$clean[ $key ] = sanitize_text_field( trim( $input[ $key ] ) );
			}
		}
		update_option( self::OPTION, $clean );
	}
}
