<?php
/**
 * Link shortener client.
 *
 * Ported from 4kbolly.py: the shortener (cinealert) answers with a variety
 * of response shapes, so every known key is probed and a raw-text body is
 * accepted as a fallback. Any failure returns the original URL rather than
 * dropping the link.
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Shortens resolved download links.
 */
class XDM_Shortener {

	/**
	 * Response keys used by the various shortener backends, in priority order.
	 */
	const KNOWN_KEYS = array(
		'shortenedUrl', // GPLinks, ShrinkMe, CineAlert
		'short_url',
		'short_link',
		'shortUrl',
		'shortened_url',
		'url',          // AdFly, minimal APIs
		'link',         // Bitly
		'result',
		'output',
	);

	/**
	 * Shorten one URL, or return it unchanged.
	 *
	 * @param string $url Destination URL.
	 * @return string
	 */
	public static function shorten( $url ) {
		$url = XDM_Regex::safe_url( $url );
		if ( '' === $url ) {
			return '';
		}
		$endpoint = XDM_Settings::shortener_endpoint();
		if ( '' === $endpoint ) {
			return $url;
		}

		$response = wp_remote_get( $endpoint . rawurlencode( $url ), array( 'timeout' => 10 ) );
		if ( is_wp_error( $response ) ) {
			return $url;
		}
		$body = (string) wp_remote_retrieve_body( $response );

		$data = json_decode( $body, true );
		if ( is_array( $data ) ) {
			$found = self::harvest( $data );
			if ( '' !== $found ) {
				return $found;
			}
			return $url;
		}

		// Not JSON: some APIs return the bare URL as text.
		$text = trim( $body );
		if ( preg_match( '#^https?://\S+$#i', $text ) && strlen( $text ) < 300 ) {
			return esc_url_raw( $text );
		}
		return $url;
	}

	/**
	 * Pull the first plausible URL out of a decoded response, including one
	 * level of nesting.
	 *
	 * @param array $data Decoded JSON.
	 * @return string Empty when nothing usable is found.
	 */
	private static function harvest( array $data ) {
		foreach ( self::KNOWN_KEYS as $key ) {
			if ( ! empty( $data[ $key ] ) && is_string( $data[ $key ] )
				&& preg_match( '#^https?://#i', $data[ $key ] ) ) {
				return esc_url_raw( $data[ $key ] );
			}
		}
		foreach ( $data as $value ) {
			if ( is_array( $value ) ) {
				$nested = self::harvest( $value );
				if ( '' !== $nested ) {
					return $nested;
				}
				continue;
			}
			if ( is_string( $value ) && preg_match( '#^https?://#i', $value ) && strlen( $value ) < 300 ) {
				return esc_url_raw( $value );
			}
		}
		return '';
	}
}
