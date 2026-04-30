"""
api.py

Authorize.Net webhook handler.

Authorize.Net's webhook URL validator rejects URLs with dots in path
segments. The standard Frappe API method URL contains dots
(/api/method/erpnext_authorizenet.api.authnet_webhook), so deployments
should expose a clean path like /authnet_webhook via an nginx rewrite.
See README for the nginx config snippet.
"""

import frappe


@frappe.whitelist(allow_guest=True, methods=["POST", "GET", "HEAD"])
def authnet_webhook(**kwargs):
	"""
	Authorize.Net webhook handler.

	Authorize.Net validates the endpoint with a probe at save-time (GET,
	HEAD, or POST without a signature). All probes return 200 so the
	endpoint saves successfully. Real webhook deliveries always include
	the X-ANET-Signature header.
	"""
	if not frappe.request:
		return {"status": "ok"}

	method = (frappe.request.method or "").upper()

	if method in ("GET", "HEAD"):
		return {"status": "ok", "service": "authorize.net webhook"}

	signature = (
		frappe.get_request_header("X-ANET-Signature") or
		frappe.get_request_header("x-anet-signature") or
		""
	)
	if not signature:
		return {"status": "ok", "service": "authorize.net webhook"}

	from erpnext_authorizenet.authorize_net_gateway.doctype.authorize_net_settings.authorize_net_settings import (
		handle_payment_callback,
	)
	return handle_payment_callback(**kwargs)
