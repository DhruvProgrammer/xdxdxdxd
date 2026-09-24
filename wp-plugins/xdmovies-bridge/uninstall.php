<?php
/**
 * Uninstall: remove plugin options and scheduled cron.
 *
 * @package XDMovies_Bridge
 */

defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

delete_option( 'xdm_bridge_settings' );
wp_clear_scheduled_hook( 'xdm_bridge_poll' );

// Remove bridge job posts and their meta.
$jobs = get_posts(
	array(
		'post_type'      => 'xdm_job',
		'post_status'    => 'any',
		'posts_per_page' => -1,
		'fields'         => 'ids',
	)
);
foreach ( $jobs as $job_id ) {
	wp_delete_post( $job_id, true );
}
