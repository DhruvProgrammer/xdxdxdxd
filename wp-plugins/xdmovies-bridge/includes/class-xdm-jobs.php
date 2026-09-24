<?php
/**
 * Job store and background poller.
 *
 * A job is a plain custom post type so the operator can see every resolve in
 * wp-admin, with the job id / status / result kept in post meta. WP-Cron ticks
 * once a minute and advances any job that is due; it also polls opportunistically
 * on admin page loads so a fresh result shows up while the operator is looking.
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Tracks bridge jobs and advances them to a terminal state.
 */
class XDM_Jobs {

	const POST_TYPE   = 'xdm_job';
	const CRON_HOOK   = 'xdm_bridge_poll';
	const META_JOB    = '_xdm_job_id';
	const META_STATUS = '_xdm_status';
	const META_URL    = '_xdm_url';
	const META_FILTER = '_xdm_filter';
	const META_RESULT = '_xdm_result';
	const META_ERROR  = '_xdm_error';
	const META_NEXT   = '_xdm_next_poll';
	const META_SOURCE = '_xdm_source_post';

	/**
	 * Register the post type and cron schedule.
	 */
	public static function init() {
		add_action( 'init', array( __CLASS__, 'register_post_type' ) );
		add_filter( 'cron_schedules', array( __CLASS__, 'cron_schedules' ) ); // phpcs:ignore WordPress.WP.CronInterval
		add_action( self::CRON_HOOK, array( __CLASS__, 'tick' ) );
		add_action( 'admin_init', array( __CLASS__, 'maybe_schedule' ) );
		// Poll while an admin is actually looking at the screen.
		add_action( 'admin_init', array( __CLASS__, 'poll_due_jobs' ), 20 );
	}

	/**
	 * Custom 60s schedule so polling is not limited to hourly.
	 *
	 * @param array $schedules Existing schedules.
	 * @return array
	 */
	public static function cron_schedules( $schedules ) {
		$schedules['xdm_minute'] = array(
			'interval' => 60,
			'display'  => __( 'Every minute (xdmovies bridge)', 'xdmovies-bridge' ),
		);
		return $schedules;
	}

	/**
	 * Register the private job post type.
	 */
	public static function register_post_type() {
		register_post_type(
			self::POST_TYPE,
			array(
				'labels'          => array(
					'name'          => __( 'Bridge Jobs', 'xdmovies-bridge' ),
					'singular_name' => __( 'Bridge Job', 'xdmovies-bridge' ),
				),
				'public'          => false,
				'show_ui'         => false,
				'capability_type' => 'post',
				'supports'        => array( 'title' ),
			)
		);
	}

	/**
	 * Ensure the recurring event exists.
	 */
	public static function maybe_schedule() {
		if ( ! wp_next_scheduled( self::CRON_HOOK ) ) {
			wp_schedule_event( time() + 60, 'xdm_minute', self::CRON_HOOK );
		}
	}

	/**
	 * Create a job and kick off the first poll.
	 *
	 * @param string $xdmovies_url Canonical xdmovies detail URL.
	 * @param string $card_filter  all|episodes|packs.
	 * @param int    $source_post  Optional originating post id.
	 * @return int|WP_Error Job post id.
	 */
	public static function create( $xdmovies_url, $card_filter = 'all', $source_post = 0 ) {
		$created = XDM_Bridge_Client::create_job( $xdmovies_url, $card_filter );
		if ( is_wp_error( $created ) ) {
			return $created;
		}
		$job_id = isset( $created['job_id'] ) ? (string) $created['job_id'] : '';
		if ( '' === $job_id ) {
			return new WP_Error( 'xdm_no_jobid', __( 'Bridge did not return a job id.', 'xdmovies-bridge' ) );
		}

		$post_id = wp_insert_post(
			array(
				'post_type'   => self::POST_TYPE,
				'post_status' => 'publish',
				'post_title'  => sprintf( '%s — %s', _x( 'Resolve', 'xdmovies-bridge', 'xdmovies-bridge' ), $xdmovies_url ),
			),
			true
		);
		if ( is_wp_error( $post_id ) ) {
			return $post_id;
		}

		update_post_meta( $post_id, self::META_JOB, $job_id );
		update_post_meta( $post_id, self::META_URL, $xdmovies_url );
		update_post_meta( $post_id, self::META_FILTER, $card_filter );
		update_post_meta( $post_id, self::META_STATUS, 'queued' );
		update_post_meta( $post_id, self::META_NEXT, time() + 5 );
		if ( $source_post ) {
			update_post_meta( $post_id, self::META_SOURCE, (int) $source_post );
			update_post_meta( $source_post, '_xdm_last_job', (int) $post_id );
		}
		return (int) $post_id;
	}

	/**
	 * Poll every job whose next-poll time has arrived.
	 */
	public static function poll_due_jobs() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		self::tick();
	}

	/**
	 * Advance all due jobs one step.
	 */
	public static function tick() {
		$jobs = get_posts(
			array(
				'post_type'      => self::POST_TYPE,
				'post_status'    => 'any',
				'posts_per_page' => 25,
				'fields'         => 'ids',
				'meta_query'     => array( // phpcs:ignore WordPress.DB.SlowDBQuery
					array(
						'key'     => self::META_STATUS,
						'value'   => array( 'queued', 'running', 'needs_human' ),
						'compare' => 'IN',
					),
				),
			)
		);
		$now = time();
		foreach ( $jobs as $job_post_id ) {
			$next = (int) get_post_meta( $job_post_id, self::META_NEXT, true );
			if ( $next && $next > $now ) {
				continue;
			}
			self::advance( $job_post_id );
		}
	}

	/**
	 * Fetch one job's status and act on it.
	 *
	 * @param int $job_post_id Local job post id.
	 */
	private static function advance( $job_post_id ) {
		$job_id = (string) get_post_meta( $job_post_id, self::META_JOB, true );
		if ( '' === $job_id ) {
			return;
		}
		$status = self::poll( $job_post_id, $job_id );
		if ( is_wp_error( $status ) ) {
			// Transient network error: back off and retry, do not fail the job.
			update_post_meta( $job_post_id, self::META_NEXT, time() + 30 );
			return;
		}

		$state = isset( $status['status'] ) ? (string) $status['status'] : '';

		if ( 'done' === $state ) {
			$result = isset( $status['result'] ) && is_array( $status['result'] ) ? $status['result'] : array();
			$posted = XDM_Writer::publish( $result );
			if ( is_wp_error( $posted ) ) {
				update_post_meta( $job_post_id, self::META_STATUS, 'failed' );
				update_post_meta( $job_post_id, self::META_ERROR, $posted->get_error_message() );
				update_post_meta( $job_post_id, self::META_NEXT, 0 );
				return;
			}
			update_post_meta( $job_post_id, self::META_STATUS, 'done' );
			update_post_meta( $job_post_id, self::META_RESULT, $posted );
			update_post_meta( $job_post_id, self::META_NEXT, 0 );
			return;
		}

		if ( 'failed' === $state || 'cancelled' === $state ) {
			update_post_meta( $job_post_id, self::META_STATUS, $state );
			update_post_meta( $job_post_id, self::META_ERROR, isset( $status['error'] ) ? (string) $status['error'] : '' );
			update_post_meta( $job_post_id, self::META_NEXT, 0 );
			return;
		}

		if ( 'needs_human' === $state ) {
			// Parked: the bot released its worker and will resume itself once
			// the checkbox is tapped. Show the link and poll less often.
			update_post_meta( $job_post_id, self::META_STATUS, 'needs_human' );
			update_post_meta( $job_post_id, self::META_ERROR, isset( $status['solve_url'] ) ? (string) $status['solve_url'] : '' );
			update_post_meta( $job_post_id, self::META_NEXT, time() + 15 );
			return;
		}

		// queued / running
		update_post_meta( $job_post_id, self::META_STATUS, $state );
		update_post_meta( $job_post_id, self::META_NEXT, time() + 5 );
	}

	/**
	 * Single poll call, returning either the decoded payload or WP_Error.
	 *
	 * @param int    $job_post_id Local job post id.
	 * @param string $job_id      Remote job id.
	 * @return array|WP_Error
	 */
	private static function poll( $job_post_id, $job_id ) {
		return XDM_Bridge_Client::get_job( $job_id );
	}

	/**
	 * Cancel a job (best effort, via the bridge DELETE).
	 *
	 * @param int $job_post_id Local job post id.
	 */
	public static function cancel( $job_post_id ) {
		$job_id = (string) get_post_meta( $job_post_id, self::META_JOB, true );
		if ( '' !== $job_id ) {
			XDM_Bridge_Client::cancel_job( $job_id );
		}
		update_post_meta( $job_post_id, self::META_STATUS, 'cancelled' );
		update_post_meta( $job_post_id, self::META_NEXT, 0 );
	}
}
