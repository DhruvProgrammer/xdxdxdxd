<?php
/**
 * Admin UI: settings screen, "Resolve" meta box, job list, AJAX handlers.
 *
 * @package XDMovies_Bridge
 */

defined( 'ABSPATH' ) || exit;

/**
 * Renders wp-admin surfaces.
 */
class XDM_Admin {

	/**
	 * Hook the admin screens.
	 */
	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'menu' ) );
		add_action( 'admin_init', array( __CLASS__, 'handle_settings_save' ) );
		add_action( 'add_meta_boxes', array( __CLASS__, 'meta_box' ) );
		add_action( 'admin_enqueue_scripts', array( __CLASS__, 'assets' ) );
		add_action( 'wp_ajax_xdm_resolve', array( __CLASS__, 'ajax_resolve' ) );
		add_action( 'wp_ajax_xdm_cancel', array( __CLASS__, 'ajax_cancel' ) );
		add_action( 'admin_notices', array( __CLASS__, 'job_notice' ) );
	}

	/**
	 * Register the settings page.
	 */
	public static function menu() {
		add_menu_page(
			__( 'xDMovies Bridge', 'xdmovies-bridge' ),
			__( 'xDMovies Bridge', 'xdmovies-bridge' ),
			'manage_options',
			'xdmovies-bridge',
			array( __CLASS__, 'render_settings' ),
			'dashicons-media-video',
			58
		);
	}

	/**
	 * Enqueue admin assets.
	 *
	 * @param string $hook Current admin page.
	 */
	public static function assets( $hook ) {
		if ( false === strpos( (string) $hook, 'xdmovies-bridge' ) && 'post.php' !== $hook && 'post-new.php' !== $hook ) {
			return;
		}
		wp_enqueue_style( 'xdm-bridge', plugins_url( 'assets/admin.css', XDM_BRIDGE_FILE ), array(), XDM_BRIDGE_VERSION );
		wp_enqueue_script( 'xdm-bridge', plugins_url( 'assets/admin.js', XDM_BRIDGE_FILE ), array( 'jquery' ), XDM_BRIDGE_VERSION, true );
		wp_localize_script(
			'xdm-bridge',
			'xdmBridge',
			array(
				'ajaxUrl' => admin_url( 'admin-ajax.php' ),
				'nonce'   => wp_create_nonce( 'xdm_bridge' ),
			)
		);
	}

	/**
	 * Handle the settings form POST.
	 */
	public static function handle_settings_save() {
		if ( ! isset( $_POST['xdm_settings_nonce'] ) ) {
			return;
		}
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'Insufficient permissions.', 'xdmovies-bridge' ) );
		}
		check_admin_referer( 'xdm_save_settings', 'xdm_settings_nonce' );
		// Pass through raw (unslashed) values only; XDM_Settings::save()
		// applies the correct per-field sanitisation (esc_url_raw for URLs,
		// sanitize_text_field for secrets). Sanitising here too would run
		// URLs through sanitize_text_field and can mangle query strings.
		$input = array();
		foreach ( array( 'bridge_base', 'bridge_key', 'tgmovies_base', 'wp_secret', 'post_author', 'shortener' ) as $key ) {
			if ( isset( $_POST[ $key ] ) ) {
				$input[ $key ] = wp_unslash( $_POST[ $key ] ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput
			}
		}
		XDM_Settings::save( $input );
		wp_safe_redirect( add_query_arg( 'xdm_saved', '1', admin_url( 'admin.php?page=xdmovies-bridge' ) ) );
		exit;
	}

	/**
	 * Render the settings screen.
	 */
	public static function render_settings() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		$notice = isset( $_GET['xdm_saved'] ) ? '<div class="notice notice-success is-dismissible"><p>' . esc_html__( 'Settings saved.', 'xdmovies-bridge' ) . '</p></div>' : '';
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'xDMovies Bridge', 'xdmovies-bridge' ); ?></h1>
			<?php echo $notice; // phpcs:ignore WordPress.Security.EscapeOutput ?>
			<p><?php esc_html_e( 'Send an xdmovies page to the resolver bot, then post the resolved links to tgmovies via the site importer.', 'xdmovies-bridge' ); ?></p>
			<form method="post" action="">
				<?php wp_nonce_field( 'xdm_save_settings', 'xdm_settings_nonce' ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th><label for="bridge_base"><?php esc_html_e( 'Bridge URL (Render app.py)', 'xdmovies-bridge' ); ?></label></th>
						<td><input type="url" class="regular-text" id="bridge_base" name="bridge_base" value="<?php echo esc_attr( XDM_Settings::bridge_base() ); ?>" placeholder="https://your-app.onrender.com"></td>
					</tr>
					<tr>
						<th><label for="bridge_key"><?php esc_html_e( 'Bridge Key', 'xdmovies-bridge' ); ?></label></th>
						<td><input type="text" class="regular-text" id="bridge_key" name="bridge_key" value="<?php echo esc_attr( XDM_Settings::bridge_key() ); ?>"></td>
					</tr>
					<tr>
						<th><label for="tgmovies_base"><?php esc_html_e( 'tgmovies Site URL', 'xdmovies-bridge' ); ?></label></th>
						<td><input type="url" class="regular-text" id="tgmovies_base" name="tgmovies_base" value="<?php echo esc_attr( XDM_Settings::tgmovies_base() ); ?>" placeholder="https://tgmovies.morimoflix.xyz"></td>
					</tr>
					<tr>
						<th><label for="wp_secret"><?php esc_html_e( 'tgmovies secret_key', 'xdmovies-bridge' ); ?></label></th>
						<td><input type="text" class="regular-text" id="wp_secret" name="wp_secret" value="<?php echo esc_attr( XDM_Settings::wp_secret() ); ?>"></td>
					</tr>
					<tr>
						<th><label for="post_author"><?php esc_html_e( 'Default author', 'xdmovies-bridge' ); ?></label></th>
						<td><input type="text" class="regular-text" id="post_author" name="post_author" value="<?php echo esc_attr( XDM_Settings::post_author() ); ?>"></td>
					</tr>
					<tr>
						<th><label for="shortener"><?php esc_html_e( 'Shortener endpoint', 'xdmovies-bridge' ); ?></label></th>
						<td><input type="text" class="large-text code" id="shortener" name="shortener" value="<?php echo esc_attr( XDM_Settings::shortener_endpoint() ); ?>" placeholder="https://cinealert.in/?api=KEY&url="><p class="description"><?php esc_html_e( 'The encoded link is appended to this value.', 'xdmovies-bridge' ); ?></p></td>
					</tr>
				</table>
				<?php submit_button(); ?>
			</form>
			<hr>
			<h2><?php esc_html_e( 'Recent jobs', 'xdmovies-bridge' ); ?></h2>
			<?php self::render_jobs_table(); ?>
		</div>
		<?php
	}

	/**
	 * Render the recent-jobs table.
	 */
	private static function render_jobs_table() {
		$jobs = get_posts(
			array(
				'post_type'      => XDM_Jobs::POST_TYPE,
				'post_status'    => 'any',
				'posts_per_page' => 20,
			)
		);
		if ( ! $jobs ) {
			echo '<p>' . esc_html__( 'No jobs yet.', 'xdmovies-bridge' ) . '</p>';
			return;
		}
		echo '<table class="widefat striped"><thead><tr>';
		echo '<th>' . esc_html__( 'Status', 'xdmovies-bridge' ) . '</th>';
		echo '<th>' . esc_html__( 'Source URL', 'xdmovies-bridge' ) . '</th>';
		echo '<th>' . esc_html__( 'Result', 'xdmovies-bridge' ) . '</th>';
		echo '</tr></thead><tbody>';
		foreach ( $jobs as $job ) {
			$status = (string) get_post_meta( $job->ID, XDM_Jobs::META_STATUS, true );
			$url    = (string) get_post_meta( $job->ID, XDM_Jobs::META_URL, true );
			$error  = (string) get_post_meta( $job->ID, XDM_Jobs::META_ERROR, true );
			$res    = get_post_meta( $job->ID, XDM_Jobs::META_RESULT, true );
			$label  = 'done' === $status && is_array( $res ) && ! empty( $res['post_url'] )
				? '<a href="' . esc_url( $res['post_url'] ) . '" target="_blank">' . esc_html__( 'View post', 'xdmovies-bridge' ) . '</a>'
				: ( $error ? esc_html( $error ) : '' );
			echo '<tr><td>' . esc_html( $status ) . '</td>';
			echo '<td><code>' . esc_html( $url ) . '</code></td>';
			echo '<td>' . $label . '</td></tr>'; // phpcs:ignore WordPress.Security.EscapeOutput
		}
		echo '</tbody></table>';
	}

	/**
	 * Register the "Resolve" meta box on the post editor.
	 */
	public static function meta_box() {
		add_meta_box(
			'xdm-resolve',
			__( 'xDMovies Resolver', 'xdmovies-bridge' ),
			array( __CLASS__, 'render_meta_box' ),
			'post',
			'side',
			'default'
		);
	}

	/**
	 * Render the "Resolve" meta box.
	 *
	 * @param WP_Post $post Current post.
	 */
	public static function render_meta_box( $post ) {
		wp_nonce_field( 'xdm_meta', 'xdm_meta_nonce' );
		$url      = '';
		$last_job = (int) get_post_meta( $post->ID, '_xdm_last_job', true );
		if ( $last_job ) {
			$url = (string) get_post_meta( $last_job, XDM_Jobs::META_URL, true );
		}
		?>
		<p><label for="xdm_url"><strong><?php esc_html_e( 'xdmovies URL', 'xdmovies-bridge' ); ?></strong></label>
			<input type="url" id="xdm_url" class="widefat" value="<?php echo esc_attr( $url ); ?>" placeholder="https://top.xdmovies.wtf/movies/..."></p>
		<p>
			<select id="xdm_filter">
				<option value="all"><?php esc_html_e( 'All links', 'xdmovies-bridge' ); ?></option>
				<option value="episodes"><?php esc_html_e( 'Episodes only', 'xdmovies-bridge' ); ?></option>
				<option value="packs"><?php esc_html_e( 'Packs / Zips only', 'xdmovies-bridge' ); ?></option>
			</select>
		</p>
		<p><button type="button" class="button button-primary" id="xdm-resolve-btn"><?php esc_html_e( 'Resolve with bot', 'xdmovies-bridge' ); ?></button></p>
		<div id="xdm-resolve-msg"></div>
		<?php if ( $last_job ) : ?>
			<p><a href="<?php echo esc_url( get_edit_post_link( $last_job ) ); ?>#xdm-resolve"><?php esc_html_e( 'View latest job', 'xdmovies-bridge' ); ?></a></p>
		<?php endif; ?>
		<?php
	}

	/**
	 * AJAX: start a resolve for the current post.
	 */
	public static function ajax_resolve() {
		check_ajax_referer( 'xdm_bridge', 'nonce' );
		$post_id = isset( $_POST['post_id'] ) ? absint( $_POST['post_id'] ) : 0;
		// Capability must be checked against THIS post, not the generic
		// 'edit_post' meta cap -- otherwise any contributor could enqueue a
		// resolve against an edit they cannot touch.
		if ( ! $post_id || ! current_user_can( 'edit_post', $post_id ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'xdmovies-bridge' ) ), 403 );
		}
		$url = isset( $_POST['url'] ) ? esc_url_raw( wp_unslash( $_POST['url'] ) ) : '';
		$filter = isset( $_POST['filter'] ) ? sanitize_key( wp_unslash( $_POST['filter'] ) ) : 'all';
		if ( ! in_array( $filter, array( 'all', 'episodes', 'packs' ), true ) ) {
			$filter = 'all';
		}
		if ( ! $url ) {
			wp_send_json_error( array( 'message' => __( 'Missing URL.', 'xdmovies-bridge' ) ), 400 );
		}
		$job = XDM_Jobs::create( $url, $filter, $post_id );
		if ( is_wp_error( $job ) ) {
			wp_send_json_error( array( 'message' => $job->get_error_message() ), 500 );
		}
		wp_send_json_success( array(
			'job'    => $job,
			'message' => __( 'Queued. The bot is working; results appear on the Bridge Jobs screen.', 'xdmovies-bridge' ),
		) );
	}

	/**
	 * AJAX: cancel a job.
	 */
	public static function ajax_cancel() {
		check_ajax_referer( 'xdm_bridge', 'nonce' );
		if ( ! current_user_can( 'edit_post' ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'xdmovies-bridge' ) ), 403 );
		}
		$job = isset( $_POST['job_id'] ) ? absint( $_POST['job_id'] ) : 0;
		if ( $job ) {
			XDM_Jobs::cancel( $job );
		}
		wp_send_json_success();
	}

	/**
	 * Surface a needs-human or failed job on the job editor.
	 */
	public static function job_notice() {
		$screen = get_current_screen();
		if ( ! $screen || XDM_Jobs::POST_TYPE !== $screen->post_type ) {
			return;
		}
		$job_id  = isset( $_GET['post'] ) ? absint( $_GET['post'] ) : 0; // phpcs:ignore WordPress.Security.NonceVerification
		$status  = (string) get_post_meta( $job_id, XDM_Jobs::META_STATUS, true );
		$error   = (string) get_post_meta( $job_id, XDM_Jobs::META_ERROR, true );
		$url     = (string) get_post_meta( $job_id, XDM_Jobs::META_URL, true );
		if ( 'needs_human' === $status && $error ) {
			echo '<div class="notice notice-warning"><p><strong>' . esc_html__( 'Cloudflare needs you:', 'xdmovies-bridge' ) . '</strong> ';
			printf(
				/* translators: %s: source URL */
				esc_html__( 'Tap the checkbox for %s to let the bot continue:', 'xdmovies-bridge' ),
				'<code>' . esc_html( $url ) . '</code>'
			);
			echo ' <a class="button button-primary" target="_blank" href="' . esc_url( $error ) . '">' . esc_html__( 'Open solve link', 'xdmovies-bridge' ) . '</a></p></div>';
		} elseif ( 'failed' === $status ) {
			echo '<div class="notice notice-error"><p><strong>' . esc_html__( 'Job failed:', 'xdmovies-bridge' ) . '</strong> ' . esc_html( $error ) . '</p></div>';
		}
	}
}
