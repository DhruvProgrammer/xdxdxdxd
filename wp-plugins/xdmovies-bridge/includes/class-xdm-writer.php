<?php
/**
 * Result writer: bot JSON -> tmovies post.
 *
 * Builds the pipe-delimited, quality-grouped link block that the tmovies
 * importer has always received, shortens each link, then calls the site's
 * own /tgmovies/v1/auto-post (or /append-links when the movie already
 * exists). No posting logic is reimplemented here beyond assembling the
 * payload the existing routes already accept.
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Formats and publishes a resolved job.
 */
class XDM_Writer {

	/**
	 * Sanitised pipe rows keyed by quality, in the importer's format:
	 * "<LABEL>|<short-url>|<size>" with a "<b>QUALITY</b>" header per group.
	 *
	 * @param array $result Bot job result payload.
	 * @return array|WP_Error quality => list of row strings
	 */
	public static function build_rows( array $result ) {
		$cards = isset( $result['cards'] ) && is_array( $result['cards'] ) ? $result['cards'] : array();
		if ( ! $cards ) {
			return new WP_Error( 'xdm_no_cards', __( 'Bot returned no cards.', 'xdmovies-bridge' ) );
		}

		$page_language = isset( $result['language'] ) ? (string) $result['language'] : '';
		$rows          = array();

		foreach ( $cards as $card ) {
			$dest = isset( $card['url'] ) ? XDM_Regex::safe_url( $card['url'] ) : '';
			if ( '' === $dest ) {
				continue; // unresolved / failed card
			}
			$title = isset( $card['raw_title'] ) ? (string) $card['raw_title'] : '';
			$size  = XDM_Regex::size( isset( $card['size'] ) ? $card['size'] : '' );

			$quality = XDM_Regex::quality( $title );
			$host    = XDM_Regex::host( $dest );

			// Per-card language: explicit card metadata first, then what the
			// filename carries, then the page-level audio block. Technical
			// source tags ride along in front of a real language so the
			// importer's quality dropdown tells releases apart
			// ("1080P NF WEB-DL ..." rather than bare "1080P").
			$language = XDM_Regex::languages( isset( $card['language'] ) ? $card['language'] : '' );
			if ( 'Unknown Audio' === $language ) {
				$language = XDM_Regex::languages( $title );
			}
			if ( 'Unknown Audio' === $language ) {
				$language = ( '' !== $page_language && 'Unknown Audio' !== $page_language )
					? $page_language
					: '';
			}
			$tags = XDM_Regex::source_tags( $title );
			if ( '' !== $tags ) {
				$language = '' !== $language ? $tags . ' ' . $language : $tags;
			}

			$label  = XDM_Regex::row_label( $quality, $language, $host );
			$short  = XDM_Shortener::shorten( $dest );
			$rows[] = array(
				// Uppercased because the importer renders this as the
				// "<b>QUALITY</b>" group header, and 4kbolly always
				// emitted "2160P", never "2160p".
				'quality' => strtoupper( $quality ),
				'row'     => $label . '|' . $short . '|' . $size,
			);
		}

		if ( ! $rows ) {
			return new WP_Error( 'xdm_all_failed', __( 'Every card failed to resolve.', 'xdmovies-bridge' ) );
		}

		// Group under <b>QUALITY</b> headers, the exact shape the importer parses.
		$grouped = array();
		foreach ( $rows as $row ) {
			$grouped[ $row['quality'] ][] = $row['row'];
		}
		$lines = array();
		foreach ( $grouped as $quality => $items ) {
			$lines[] = '<b>' . esc_html( $quality ) . '</b>';
			foreach ( $items as $item ) {
				$lines[] = $item;
			}
		}
		return implode( "\n", $lines );
	}

	/**
	 * Resolve the movie_name the importer keys off: TMDB id when the bot
	 * found one, otherwise the clean title.
	 *
	 * @param array $result Bot job result payload.
	 * @return string
	 */
	public static function movie_name( array $result ) {
		$tmdb = isset( $result['tmdb_id'] ) ? (string) $result['tmdb_id'] : '';
		if ( '' !== $tmdb ) {
			return $tmdb;
		}
		$title = isset( $result['title'] ) ? (string) $result['title'] : '';
		return '' !== $title ? $title : __( 'Untitled', 'xdmovies-bridge' );
	}

	/**
	 * Page-level language, with the importer's sentinel when unknown.
	 *
	 * @param array $result Bot job result payload.
	 * @return string
	 */
	public static function language( array $result ) {
		$language = isset( $result['language'] ) ? (string) $result['language'] : '';
		return '' !== $language ? $language : 'Unknown Audio';
	}

	/**
	 * Post the result to tmovies via the site's auto-post route, falling
	 * back to append-links when the movie already exists.
	 *
	 * @param array $result Bot job result payload.
	 * @return array|WP_Error {mode, ok, post_url, message}
	 */
	public static function publish( array $result ) {
		$rows = self::build_rows( $result );
		if ( is_wp_error( $rows ) ) {
			return $rows;
		}

		$payload = array(
			'secret_key'  => XDM_Settings::wp_secret(),
			'movie_name'  => self::movie_name( $result ),
			'links'       => $rows,
			'author'      => XDM_Settings::post_author(),
			'language'    => self::language( $result ),
			'merge_links' => true,
		);

		$response = self::post_to_tgmovies( 'auto-post', $payload );
		if ( ! is_wp_error( $response ) && ! empty( $response['success'] ) ) {
			return array(
				'mode'     => 'auto-post',
				'ok'       => true,
				'post_url' => isset( $response['post_url'] ) ? $response['post_url'] : '',
				'message'  => isset( $response['title'] ) ? $response['title'] : '',
			);
		}

		$message = is_wp_error( $response )
			? $response->get_error_message()
			: ( isset( $response['message'] ) ? (string) $response['message'] : '' );

		// The importer reports "already imported" on a duplicate; the
		// correct follow-up is to append the new links to the live post.
		if ( self::is_already_imported( $message ) ) {
			unset( $payload['author'], $payload['merge_links'] );
			$appended = self::post_to_tgmovies( 'append-links', $payload );
			if ( ! is_wp_error( $appended ) && ! empty( $appended['success'] ) ) {
				return array(
					'mode'     => 'append-links',
					'ok'       => true,
					'post_url' => isset( $appended['post_url'] ) ? $appended['post_url'] : '',
					'message'  => isset( $appended['title'] ) ? $appended['title'] : '',
				);
			}
			return new WP_Error( 'xdm_append_failed', is_wp_error( $appended ) ? $appended->get_error_message() : $message );
		}

		return new WP_Error( 'xdm_post_failed', '' !== $message ? $message : __( 'tgmovies rejected the post.', 'xdmovies-bridge' ) );
	}

	/**
	 * Whether a failure message means "this movie is already on the site".
	 *
	 * @param string $message Error text from the importer.
	 * @return bool
	 */
	private static function is_already_imported( $message ) {
		$message = strtolower( (string) $message );
		foreach ( array( 'already', 'imported', 'exists', 'duplicate' ) as $needle ) {
			if ( false !== strpos( $message, $needle ) ) {
				return true;
			}
		}
		return false;
	}

	/**
	 * Call one of the tmovies REST routes.
	 *
	 * @param string $route    auto-post|append-links.
	 * @param array  $payload  JSON body.
	 * @return array|WP_Error
	 */
	private static function post_to_tgmovies( $route, array $payload ) {
		$base = XDM_Settings::tgmovies_base();
		if ( '' === $base ) {
			return new WP_Error( 'xdm_no_tgmovies', __( 'tgmovies site URL is not configured.', 'xdmovies-bridge' ) );
		}
		$url = untrailingslashit( $base ) . '/?rest_route=/tgmovies/v1/' . $route;

		$response = wp_remote_post(
			$url,
			array(
				'timeout' => 30,
				'headers' => array( 'Content-Type' => 'application/json' ),
				'body'    => wp_json_encode( $payload ),
			)
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}
		$data = json_decode( (string) wp_remote_retrieve_body( $response ), true );
		return is_array( $data ) ? $data : new WP_Error( 'xdm_bad_json', __( 'tgmovies returned invalid JSON.', 'xdmovies-bridge' ) );
	}
}
