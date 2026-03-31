"""
nmi_settings.py

Controller for the NMI Settings DocType.
Implements the standard Frappe payment gateway interface using NMI's
Three-Step Redirect API.

NMI Three-Step Redirect flow:
  Step 1: POST to NMI with transaction details → receive a form-post-URL + token
  Step 2: Redirect customer to NMI's hosted payment page to enter card details
  Step 3: NMI POSTs result back to our callback URL → we complete the payment

NMI API docs: https://secure.nmi.com/merchants/resources/integration/integration_portal.php
Security key authentication — no username/password needed in requests.

NOTE: Activate this gateway once TMS provides NMI credentials.
      The Three-Step API endpoint and payload structure below are correct
      per NMI's current documentation but should be verified against
      the specific NMI reseller instance TMS provisions.
"""

import json
import frappe
import requests
from frappe import _
from frappe.utils import get_url, call_hook_method
from urllib.parse import urlencode


NMI_LIVE_URL = "https://secure.nmi.com/api/v2/three-step"
NMI_SANDBOX_URL = "https://secure.nmi.com/api/v2/three-step"  # NMI uses the same endpoint; sandbox via credentials

SUPPORTED_CURRENCIES = ["USD", "CAD"]


class NMISettings(frappe.model.document.Document):

	def validate(self):
		"""Called on Save. Registers this gateway instance."""
		self._register_gateway()
		call_hook_method("payment_gateway_enabled", gateway=f"NMI-{self.gateway_name}")

	def _register_gateway(self):
		from payments.utils import create_payment_gateway

		create_payment_gateway(
			f"NMI-{self.gateway_name}",
			settings="NMI Settings",
			controller=self.gateway_name,
		)

	# ------------------------------------------------------------------
	# Standard Frappe gateway interface
	# ------------------------------------------------------------------

	def validate_transaction_currency(self, currency):
		if currency not in SUPPORTED_CURRENCIES:
			frappe.throw(
				_(
					"NMI does not support transactions in {0} through this integration. "
					"Supported: {1}"
				).format(currency, ", ".join(SUPPORTED_CURRENCIES))
			)

	def get_payment_url(self, **kwargs):
		integration_request = self.create_request(kwargs)
		return get_url(
			f"./nmi_checkout?{urlencode({'req': integration_request.name, 'gateway': self.gateway_name})}"
		)

	def create_request(self, data):
		self.data = frappe._dict(data)

		integration_request = frappe.get_doc({
			"doctype": "Integration Request",
			"integration_type": "Remote",
			"integration_request_service": f"NMI-{self.gateway_name}",
			"reference_doctype": self.data.get("reference_doctype"),
			"reference_docname": self.data.get("reference_docname"),
			"data": json.dumps(self.data),
			"status": "Queued",
		})
		integration_request.insert(ignore_permissions=True)
		frappe.db.commit()

		return integration_request

	def get_step1_response(self, integration_request_name):
		"""
		NMI Three-Step Step 1:
		POST transaction details to NMI → receive redirect URL and token.
		Returns dict with 'form-url' and 'token-id'.
		"""
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		data = frappe._dict(json.loads(integration_request.data))

		security_key = self.get_password("security_key")
		base_url = get_url()
		callback_url = (
			f"{base_url}/api/method/"
			"erpnext_authorizenet.payment_gateways.doctype.nmi_settings.nmi_settings.handle_payment_callback"
		)

		amount = frappe.utils.flt(data.get("amount") or data.get("grand_total"), 2)
		email = data.get("payer_email") or data.get("email") or ""
		first_name = (data.get("payer_name") or data.get("customer_name") or "Customer").split()[0]
		last_name = " ".join(
			(data.get("payer_name") or data.get("customer_name") or "").split()[1:]
		) or "."

		payload = {
			"security-key": security_key,
			"amount": f"{amount:.2f}",
			"currency": data.get("currency", "USD"),
			"order-id": integration_request_name,  # echoed back by NMI on Step 3
			"redirect-url": callback_url,
			"billing-first-name": first_name,
			"billing-last-name": last_name,
			"billing-email": email,
			"type": "sale",
		}

		try:
			response = requests.post(
				NMI_LIVE_URL if not self.sandbox_mode else NMI_SANDBOX_URL,
				data=payload,
				timeout=15,
			)
			response.raise_for_status()
		except requests.exceptions.RequestException as e:
			frappe.log_error(title="NMI Step 1 Connection Error", message=str(e))
			frappe.throw(_("Could not connect to NMI payment gateway. Please try again."))

		# NMI returns XML — parse it
		import xml.etree.ElementTree as ET
		try:
			root = ET.fromstring(response.text)
		except ET.ParseError as e:
			frappe.log_error(title="NMI Step 1 XML Parse Error", message=response.text)
			frappe.throw(_("Unexpected response from NMI gateway."))

		result_code = root.findtext("result")
		if result_code != "1":
			error_text = root.findtext("result-text") or "Unknown error"
			frappe.log_error(title="NMI Step 1 Error", message=error_text)
			frappe.throw(_("NMI gateway error: {0}").format(error_text))

		form_url = root.findtext("form-url")
		token_id = root.findtext("token-id")

		if not form_url or not token_id:
			frappe.throw(_("NMI did not return a payment URL. Check Security Key."))

		# Store token on integration request for reference
		integration_request.db_set("output", token_id, update_modified=False)
		frappe.db.commit()

		return {"form_url": form_url, "token_id": token_id}


# ------------------------------------------------------------------
# NMI Step 3 webhook callback
# ------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def handle_payment_callback(**kwargs):
	"""
	NMI POSTs here after the customer submits card details (Step 3).
	Expected POST fields: token-id, order-id (our Integration Request name)

	After receiving the token, we make a Step 3 server-side POST to NMI
	to complete the transaction, then record the payment in ERPNext.
	"""
	import xml.etree.ElementTree as ET

	form = frappe.request.form if hasattr(frappe, "request") else frappe._dict(kwargs)

	token_id = form.get("token-id") or kwargs.get("token-id")
	order_id = form.get("order-id") or kwargs.get("order-id")  # = Integration Request name

	if not token_id or not order_id:
		frappe.log_error(
			title="NMI Callback: Missing token-id or order-id",
			message=str(dict(form)),
		)
		frappe.respond_as_web_page(
			_("Payment Error"),
			_("Could not identify the payment. Please contact support."),
			indicator_color="red",
		)
		return

	try:
		integration_request = frappe.get_doc("Integration Request", order_id)
	except frappe.DoesNotExistError:
		frappe.log_error(
			title="NMI Callback: Integration Request not found",
			message=f"order-id={order_id}",
		)
		frappe.respond_as_web_page(
			_("Payment Error"),
			_("Payment record not found."),
			indicator_color="red",
		)
		return

	data = frappe._dict(json.loads(integration_request.data))
	gateway_name = data.get("payment_gateway", "").replace("NMI-", "") or integration_request.integration_request_service.replace("NMI-", "")

	try:
		settings = frappe.get_doc("NMI Settings", gateway_name)
	except frappe.DoesNotExistError:
		frappe.log_error(title="NMI Callback: Settings not found", message=gateway_name)
		frappe.respond_as_web_page(
			_("Configuration Error"),
			_("Payment gateway configuration not found."),
			indicator_color="red",
		)
		return

	# Step 3: Complete the transaction server-side
	security_key = settings.get_password("security_key")
	step3_payload = {
		"security-key": security_key,
		"token-id": token_id,
	}

	try:
		response = requests.post(
			NMI_LIVE_URL if not settings.sandbox_mode else NMI_SANDBOX_URL,
			data=step3_payload,
			timeout=15,
		)
		response.raise_for_status()
	except requests.exceptions.RequestException as e:
		frappe.log_error(title="NMI Step 3 Connection Error", message=str(e))
		frappe.respond_as_web_page(
			_("Payment Error"),
			_("Could not complete payment with gateway. Please contact support."),
			indicator_color="red",
		)
		return

	try:
		root = ET.fromstring(response.text)
	except ET.ParseError:
		frappe.log_error(title="NMI Step 3 XML Error", message=response.text)
		frappe.respond_as_web_page(
			_("Payment Error"),
			_("Unexpected gateway response."),
			indicator_color="red",
		)
		return

	result_code = root.findtext("result")
	transaction_id = root.findtext("transactionid") or ""
	result_text = root.findtext("result-text") or ""

	if result_code == "1":
		# Approved
		_finalize_payment(integration_request, data, transaction_id)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url(
			f"./payment-success?doctype={data.get('reference_doctype')}&docname={data.get('reference_docname')}"
		)
	else:
		integration_request.db_set("status", "Failed", update_modified=False)
		frappe.db.commit()
		frappe.respond_as_web_page(
			_("Payment Declined"),
			_("Your payment was not approved: {0}. Please try again.").format(result_text),
			indicator_color="red",
		)


def _finalize_payment(integration_request, data, transaction_id):
	try:
		integration_request.db_set("status", "Completed", update_modified=False)
		integration_request.db_set(
			"output",
			json.dumps({"transactionid": transaction_id}),
			update_modified=False,
		)
		frappe.db.commit()

		if data.get("reference_doctype") == "Payment Request":
			payment_request = frappe.get_doc("Payment Request", data.get("reference_docname"))
			payment_request.run_method("on_payment_authorized", "Completed")
			frappe.db.commit()

	except Exception as e:
		frappe.log_error(
			title="NMI: Payment finalization error",
			message=f"Integration Request: {integration_request.name}\nTransaction: {transaction_id}\nError: {str(e)}",
		)
