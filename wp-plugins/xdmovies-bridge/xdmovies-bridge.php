<?php
/**
 * Plugin Name:       xDMovies Resolver Bridge
 * Plugin URI:        https://tgmovies.morimoflix.xyz
 * Description:       Send an xdmovies page to the resolver bot, then post the resolved links to tgmories through the site's TMDB importer. Uses a Render bridge so this WordPress install never holds the bot API key.
 * Version:           1.0.0
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Author:            xDMovies
 * Text Domain:       xdmovies-bridge
 * License:           GPL-2.0-or-later
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

define( 'XDM_BRIDGE_VERSION', '1.0.0' );
define( 'XDM_BRIDGE_FILE', __FILE__ );
define( 'XDM_BRIDGE_DIR', plugin_dir_path( __FILE__ ) );
define( 'XDM_BRIDGE_URL', plugin_dir_url( __FILE__ ) );

require_once XDM_BRIDGE_DIR . 'includes/class-xdm-regex.php';
require_once XDM_BRIDGE_DIR . 'includes/class-xdm-settings.php';
require_once XDM_BRIDGE_DIR . 'includes/class-xdm-shortener.php';
require_once XDM_BRIDGE_DIR . 'includes/class-xdm-bridge-client.php';
require_once XDM_BRIDGE_DIR . 'includes/class-xdm-writer.php';
require_once XDM_BRIDGE_DIR . 'includes/class-xdm-jobs.php';
require_once XDM_BRIDGE_DIR . 'includes/class-xdm-admin.php';

/**
 * Boot the plugin subsystems.
 */
function xdm_bridge_bootstrap() {
	XDM_Jobs::init();
	if ( is_admin() ) {
		XDM_Admin::init();
	}
}
add_action( 'plugins_loaded', 'xdm_bridge_bootstrap' );

/**
 * Clean up cron on deactivation.
 */
function xdm_bridge_deactivate() {
	wp_clear_scheduled_hook( XDM_Jobs::CRON_HOOK );
}
register_deactivation_hook( __FILE__, 'xdm_bridge_deactivate' );
