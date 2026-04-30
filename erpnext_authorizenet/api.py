"""
api.py

Short-path aliases for external webhook callbacks.
Authorize.Net's webhook URL field rejects URLs containing dots in path
segments (it treats them as suspicious file-extension-like paths).

This module provides:
  1. authnet_webhook() — the actual handler, callable at the Frappe
     /api/method/... path
  2. handle_clean_path_webhook() — a before_request hook that catches
     requests to /authnet_webhook and routes them to the handler,
     so no nginx-level rewrite is needed.

Public URL (clean): https://YOUR-DOMAIN/authnet_webhook
Internal URL:       https://YOUR-DOMAIN/api/method/erpnext_authorizenet.api.authnet_webhook
Both work. The clean one is what Authorize.Net accepts.
"""

import json
import frappe


# Path that Authorize.Net hits — must contain no dots
WEBHOOK_CLEAN_PATH = "/authnet_webhook"


def handle_clean_path_webhook():
	"""
	Registered as a `before_request` hook in hooks.py.
	Frappe calls this on every incoming request. If the path matches
	/authnet_webhook, we hijack the request and run the handler ourselves,
	then short-circuit Frappe's normal routing by raising a redirect-like
	response.
	"""
	if not frappe.request:
		return

	path = (frappe.request.path or "").rstrip("/")
	if path != WEBHOOK_CLEAN_PATH:
		return

	# Run the handler
	result = authnet_webhook()

	# Build a JSON response and short-circuit Frappe's request handler
	frappe.local.response["type"] = "json"
	frappe.local.response["http_status_code"] = 200
	if isinstance(result, dict):
		# If the handler returned a status dict, surface it at the top level
		# (not wrapped in {"message": ...}) so Authorize.Net sees a clean ack.
		for k, v in result.items():
			frappe.local.response[k] = v


@frappe.whitelist(allow_guest=True, methods=["POST", "GET", "HEAD"])
def authnet_webhook(**kwargs):
	"""
	Authorize.Net webhook handler.

	Accessible at:
	  - /authnet_webhook                                    (via before_request hook)
	  - /api/method/erpnext_authorizenet.api.authnet_webhook (standard Frappe API)

	Authorize.Net validates the endpoint with a probe at save-time. We've
	seen them probe with GET, HEAD, and POST (with no signature). Any of
	these should return 200 so the endpoint saves successfully. Real
	webhook deliveries always include the X-ANET-Signature header — that's
	how we distinguish them from validation probes.
	"""
	if not frappe.request:
		return {"status": "ok"}

	method = (frappe.request.method or "").upper()

	# GET / HEAD = simple probe, return OK
	if method in ("GET", "HEAD"):
		return {"status": "ok", "service": "authorize.net webhook"}

	# POST without a signature header = probably a validation probe.
	# Real webhook deliveries from Authorize.Net always include X-ANET-Signature.
	signature = (
		frappe.get_request_header("X-ANET-Signature") or
		frappe.get_request_header("x-anet-signature") or
		""
	)
	if not signature:
		return {"status": "ok", "service": "authorize.net webhook"}

	# Real webhook — pass to the verified handler
	from erpnext_authorizenet.authorize_net_gateway.doctype.authorize_net_settings.authorize_net_settings import (
		handle_payment_callback,
	)
	return handle_payment_callback(**kwargs)
