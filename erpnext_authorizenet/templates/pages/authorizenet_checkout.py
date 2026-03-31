"""
authorizenet_checkout.py

The page the customer lands on after clicking the payment link.
Fetches a hosted payment token from Authorize.Net and renders the iframe.
"""

import frappe
from frappe import _
import json


def get_context(context):
	context.no_cache = 1
	context.no_breadcrumbs = 1

	req_name = frappe.form_dict.get("req")
	gateway_name = frappe.form_dict.get("gateway")
	cancelled = frappe.form_dict.get("cancelled")

	if cancelled:
		context.cancelled = True
		return context

	if not req_name or not gateway_name:
		frappe.throw(_("Invalid payment link."), frappe.PermissionError)

	# Load the settings doc for this gateway instance
	try:
		settings = frappe.get_doc("Authorize Net Settings", gateway_name)
	except frappe.DoesNotExistError:
		frappe.throw(
			_("Authorize.Net gateway '{0}' is not configured.").format(gateway_name)
		)

	# Get the hosted payment token (calls Authorize.Net API)
	token = settings.get_hosted_payment_token(req_name)

	integration_request = frappe.get_doc("Integration Request", req_name)
	data = frappe._dict(json.loads(integration_request.data))

	context.token = token
	context.hosted_form_url = settings.get_hosted_form_url()
	context.sandbox_mode = settings.sandbox_mode
	context.integration_request = req_name
	context.gateway_name = gateway_name
	context.amount = data.get("amount") or data.get("grand_total")
	context.currency = data.get("currency", "USD")
	context.description = data.get("description") or f"Payment for {data.get('reference_docname', '')}"
	context.payer_name = data.get("payer_name") or data.get("customer_name") or ""
	context.title = "Secure Payment"

	return context
