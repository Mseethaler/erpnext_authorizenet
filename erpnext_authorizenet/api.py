"""
api.py

Authorize.Net public endpoints:
  - authnet_webhook : signed JSON webhook (failsafe path)
  - authnet_return  : redirect-back from hosted payment form (primary path)
  - authnet_cancel  : redirect-back when customer clicks Cancel on hosted form

Authorize.Net's URL validators reject URLs with dots in path segments. The
standard Frappe API method URL contains dots
(/api/method/erpnext_authorizenet.api.authnet_webhook), so deployments
expose clean paths via nginx rewrites. See README for the nginx config.
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


@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def authnet_return(**kwargs):
	"""
	Authorize.Net redirect-back handler.

	Customer is sent here by the hosted payment form after entering card
	details. Authorize.Net POSTs the form body containing transId, refId,
	and response_code. We finalize the payment and redirect to the
	confirmation page. This is the PRIMARY payment recording path; the
	webhook acts as a failsafe.
	"""
	from erpnext_authorizenet.authorize_net_gateway.doctype.authorize_net_settings.authorize_net_settings import (
		handle_payment_return,
	)
	return handle_payment_return(**kwargs)


@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def authnet_cancel(**kwargs):
	"""
	Authorize.Net cancel-redirect handler.

	Customer is sent here when they click Cancel on the hosted payment form.
	We mark the Integration Request as Cancelled (if still pending) and
	redirect to the confirmation page with cancelled=1.
	"""
	from erpnext_authorizenet.authorize_net_gateway.doctype.authorize_net_settings.authorize_net_settings import (
		handle_payment_cancel,
	)
	return handle_payment_cancel(**kwargs)
