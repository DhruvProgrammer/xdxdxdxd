/**
 * Editor meta box behaviour for the xDMovies Resolver Bridge.
 */
( function ( $ ) {
	'use strict';

	$( function () {
		var $btn = $( '#xdm-resolve-btn' );
		var $msg = $( '#xdm-resolve-msg' );

		$btn.on( 'click', function () {
			var postId = $( '#post_ID' ).val();
			var url = ( $( '#xdm_url' ).val() || '' ).trim();
			var filter = $( '#xdm_filter' ).val() || 'all';

			if ( ! postId ) {
				$msg.html( '<span style="color:#b32d2e;">Save the post first.</span>' );
				return;
			}
			if ( ! /^https:\/\/([a-z0-9-]+\.)*xdmovies\.wtf\/(movies|series)\//i.test( url ) ) {
				$msg.html( '<span style="color:#b32d2e;">Enter an https://…xdmovies.wtf/movies/… or /series/… URL.</span>' );
				return;
			}

			$btn.prop( 'disabled', true ).text( 'Queueing…' );
			$msg.html( '<span>Sending to the bridge…</span>' );

			$.post( window.xdmBridge.ajaxUrl, {
				action: 'xdm_resolve',
				nonce: window.xdmBridge.nonce,
				post_id: postId,
				url: url,
				filter: filter,
			} ).done( function ( res ) {
				if ( res && res.success ) {
					$msg.html( '<span style="color:#007017;">' + ( res.data.message || 'Queued.' ) + '</span>' );
				} else {
					var m = ( res && res.data && res.data.message ) || 'Request failed.';
					$msg.html( '<span style="color:#b32d2e;">' + m + '</span>' );
				}
			} ).fail( function () {
				$msg.html( '<span style="color:#b32d2e;">Network error.</span>' );
			} ).always( function () {
				$btn.prop( 'disabled', false ).text( 'Resolve with bot' );
			} );
		} );
	} );
}( jQuery ) );
