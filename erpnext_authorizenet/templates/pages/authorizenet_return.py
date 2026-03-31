"""
authorizenet_return.py

Landing page shown after Authorize.Net redirects the customer back.
This is a simple confirmation display page — the actual payment
recording happens in the webhook (handle_payment_callback).
"""

import frappe
from frappe import _


def get_context(context):
	context.no_cache = 1
	context.no_breadcrumbs = 1

	doctype = frappe.form_dict.get("doctype")
	docname = frappe.form_dict.get("docname")
	pending = frappe.form_dict.get("pending")

	context.doctype = doctype
	context.docname = docname
	context.pending = pending
	context.title = _("Payment Confirmed") if not pending else _("Payment Pending")

	if doctype and docname:
		try:
			doc = frappe.get_doc(doctype, docname)
			context.reference_doc = doc
		except Exception:
			pass

	return context
