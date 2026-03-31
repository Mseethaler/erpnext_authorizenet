"""
nmi_checkout.py

Customer-facing checkout page for NMI Three-Step Redirect.
Performs Step 1 (get redirect URL from NMI) then sends the customer there.
"""

import frappe
from frappe import _
import json


def get_context(context):
	context.no_cache = 1
	context.no_breadcrumbs = 1

	req_name = frappe.form_dict.get("req")
	gateway_name = frappe.form_dict.get("gateway")

	if not req_name or not gateway_name:
		frappe.throw(_("Invalid payment link."), frappe.PermissionError)

	try:
		settings = frappe.get_doc("NMI Settings", gateway_name)
	except frappe.DoesNotExistError:
		frappe.throw(
			_("NMI gateway '{0}' is not configured.").format(gateway_name)
		)

	# Step 1: Get NMI redirect URL
	result = settings.get_step1_response(req_name)

	# Redirect immediately to NMI hosted page — no need to render a template
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = result["form_url"]

	return context
