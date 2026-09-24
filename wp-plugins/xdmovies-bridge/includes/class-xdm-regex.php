<?php
/**
 * Release-text parsing helpers.
 *
 * Ported deliberately from the proven 4kbolly.py so the label format the
 * tmovies importer expects does not change. These are the functions that
 * turn a raw release filename into the quality / language / source / host
 * columns used in the pipe-delimited link rows.
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Pure string helpers. No DB, no HTTP.
 */
class XDM_Regex {

	/**
	 * Language keywords recognised in release titles.
	 */
	const LANG_KEYWORDS = array(
		'hindi', 'english', 'tamil', 'telugu', 'malayalam', 'kannada',
		'korean', 'japanese', 'chinese', 'spanish', 'french', 'german',
		'italian', 'russian', 'portuguese', 'arabic', 'turkish', 'thai',
		'urdu', 'dual audio', 'multi audio',
		'bengali', 'marathi', 'punjabi', 'gujarati',
	);

	/**
	 * Separator-glued DUAL inside a filename means dual audio. The plain
	 * word in a title ("Dual (2022)") or the "Dual Audio" phrase must not
	 * trip this; the latter is already covered by LANG_KEYWORDS.
	 */
	const DUAL_IN_FILENAME = '/(?<=[.\-_\[])DUAL(?=[.\-_\]])/i';

	/**
	 * Technical release tags, in display order. Only these are ever picked,
	 * so release-group names (PrimeFix, PSA, XDMovies, ...) cannot leak
	 * into the label.
	 */
	const SOURCE_TAGS = array(
		'AMZN', 'NF', 'DSNP', 'HMAX', 'ATVP',
		'WEB-DL', 'WEBDL', 'WEBRip', 'BluRay', 'BRRip',
		'HDRip', 'HDTV', 'DVDRip', 'REMUX',
		'DDP?\d(?:\.\d+)?', 'AAC\d(?:\.\d+)?', 'AC-?3',
		'DTS(?:-?HD)?(?:\s?MA)?(?:\s?\d\.\d)?', 'TrueHD(?:\s?\d\.\d)?', 'FLAC', 'Atmos',
		'H\.?265', 'H\.?264', 'x265', 'x264',
		'HEVC', 'AV1', '10BIT',
	);

	/**
	 * Case-sensitive tags scanned first: a case-insensitive "it" would match
	 * the English word in titles like "It (2017)".
	 */
	const SOURCE_TAGS_CASE = array(
		'/(?<![A-Za-z])iT(?![A-Za-z])/' => 'IT',
	);

	/**
	 * Canonical spellings for source tags.
	 */
	const SOURCE_CANON = array(
		'WEBDL'   => 'WEB-DL',
		'H264'    => 'H.264',
		'H265'    => 'H.265',
	);

	/**
	 * Host label shown in brackets. Order matters: the first substring hit
	 * wins, so more specific names come before generic ones.
	 */
	const HOST_HINTS = array(
		'gdflix'          => 'GDFlix',
		'fastdlserver'    => 'GDFlix',
		'hubcloud'        => 'HubCloud',
		'hubdrive'        => 'HubDrive',
		'drivehub'        => 'DriveHub',
		'hubx'            => 'HubX',
		'filepress'       => 'FilePress',
		'gdtot'           => 'GDToT',
		'nexdrive'        => 'NexDrive',
		'linksmod'        => 'LinksMod',
		'googleusercontent' => 'GDrive',
		'workers.dev'     => 'GDrive',
		'dotflix'         => 'GDrive',
		'dtflix'          => 'GDrive',
	);

	/**
	 * Quality: 4K last with a word boundary so "4KHDHub" cannot match.
	 *
	 * @param string $text Release title or filename.
	 * @return string
	 */
	public static function quality( $text ) {
		$text = (string) $text;
		if ( '' === $text ) {
			return 'Unknown Quality';
		}
		$patterns = array(
			'2160p' => '/2160p/i',
			'1080p' => '/1080p/i',
			'720p'  => '/720p/i',
			'540p'  => '/540p/i',
			'480p'  => '/480p/i',
			'4K'    => '/\b4k\b/i',
		);
		foreach ( $patterns as $label => $pattern ) {
			if ( preg_match( $pattern, $text ) ) {
				return $label;
			}
		}
		return 'Unknown Quality';
	}

	/**
	 * Extract an audio-language list from release text.
	 *
	 * @param string $text Release text.
	 * @return string Comma-separated, or "Unknown Audio".
	 */
	public static function languages( $text ) {
		$text  = (string) $text;
		$found = array();
		if ( preg_match_all( '/\b(' . implode( '|', array_map( 'preg_quote', self::LANG_KEYWORDS ) ) . ')\b/i', $text, $m ) ) {
			foreach ( $m[1] as $hit ) {
				$found[] = $hit;
			}
		}
		$found = array_values( array_unique( array_map( 'ucfirst', $found ) ) );
		if ( $found ) {
			$specific = array();
			$generic  = array();
			foreach ( $found as $lang ) {
				if ( in_array( strtolower( $lang ), array( 'dual audio', 'multi audio' ), true ) ) {
					$generic[] = $lang;
				} else {
					$specific[] = $lang;
				}
			}
			// Real languages win over "Dual Audio" alone: a page listing
			// "Hindi, English, Dual Audio" must not collapse to just
			// "Dual Audio".
			return $specific ? implode( ', ', $specific ) : implode( ', ', $generic );
		}
		if ( preg_match( self::DUAL_IN_FILENAME, $text ) ) {
			return 'Dual Audio';
		}
		return 'Unknown Audio';
	}

	/**
	 * Technical source/audio/video tag line for a release filename.
	 *
	 * "Twilight.of.the.Warriors.Walled.In.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-PrimeFix.mkv"
	 *   -> "AMZN WEB-DL DDP5.1 H.264"
	 *
	 * @param string $text Release filename.
	 * @return string Empty string when nothing matches.
	 */
	public static function source_tags( $text ) {
		$text = (string) $text;
		if ( '' === $text ) {
			return '';
		}
		// "DDP 5.1" -> "DDP5.1" so the tag pattern sees one token.
		$text = preg_replace( '/\b(DDP?|DTS|AAC|AC)\s+(\d)/i', '$1$2', $text );
		$out  = array();
		$add  = static function ( $raw ) use ( &$out ) {
			$tok = strtoupper( preg_replace( '/\s+/', '', $raw ) );
			$tok = isset( self::SOURCE_CANON[ $tok ] ) ? self::SOURCE_CANON[ $tok ] : $tok;
			if ( ! in_array( $tok, $out, true ) ) {
				$out[] = $tok;
			}
		};
		foreach ( self::SOURCE_TAGS_CASE as $pattern => $canon ) {
			if ( preg_match( $pattern, $text ) ) {
				$add( $canon );
			}
		}
		foreach ( self::SOURCE_TAGS as $tag ) {
			if ( preg_match( '/\b' . $tag . '\b/i', $text, $m ) ) {
				$add( $m[0] );
			}
		}
		return implode( ' ', $out );
	}

	/**
	 * Bracketed host label for a destination URL.
	 *
	 * @param string $url Destination URL.
	 * @return string
	 */
	public static function host( $url ) {
		$url = (string) $url;
		foreach ( self::HOST_HINTS as $needle => $label ) {
			if ( false !== stripos( $url, $needle ) ) {
				return $label;
			}
		}
		return 'Direct';
	}

	/**
	 * The size badge for a card, normalised to "1.21 GB" style.
	 *
	 * @param string $text Raw size.
	 * @return string
	 */
	public static function size( $text ) {
		$text = (string) $text;
		return preg_match( '/(\d+(?:\.\d+)?\s*(?:GB|MB))/i', $text, $m ) ? strtoupper( trim( $m[1] ) ) : '';
	}

	/**
	 * Build the label that prefixes each pipe row.
	 *
	 * Format: "<QUALITY> [<TAGS> <LANG>] [<HOST>]" uppercased, matching what
	 * the tmovies importer has always received.
	 *
	 * @param string $quality   Quality label.
	 * @param string $language  Language string.
	 * @param string $host      Host label.
	 * @return string
	 */
	public static function row_label( $quality, $language, $host ) {
		$parts = array_filter(
			array(
				trim( (string) $quality ),
				trim( (string) $language ),
			),
			static function ( $p ) {
				return '' !== $p;
			}
		);
		$label = trim( implode( ' ', $parts ) . ' [' . $host . ']' );
		return strtoupper( preg_replace( '/\s+/', ' ', $label ) );
	}

	/**
	 * Only http(s) URLs are ever emitted downstream.
	 *
	 * @param string $url Candidate URL.
	 * @return string Empty when the URL is not usable.
	 */
	public static function safe_url( $url ) {
		$url = trim( (string) $url );
		if ( '' === $url ) {
			return '';
		}
		if ( ! preg_match( '#^https?://#i', $url ) ) {
			return '';
		}
		return esc_url_raw( $url );
	}
}
