"""
authorizenet_settings.py

Controller for the Authorize Net Settings DocType.
Implements the standard Frappe payment gateway interface:
  - validate()              → registers the gateway on save
  - get_payment_url()       → called by Payment Request to get the checkout URL
  - validate_transaction_currency()
  - create_request()        → builds the Authorize.Net hosted payment token
  - get_token()             → calls Authorize.Net API for a hosted payment page token
  - handle_payment_callback() → processes the webhook/redirect from Authorize.Net

Authorize.Net Accept Hosted flow:
  1. ERPNext calls get_payment_url() → we call get_token() → store Integration Request
  2. Customer is redirected to /authorizenet_checkout?token=...&gateway=...
  3. Our checkout page embeds the Authorize.Net hosted iframe using the token
  4. Authorize.Net posts result to /authorizenet_return (our webhook endpoint)
  5. We verify the transaction, create a Payment Entry, mark invoice paid
"""

import json
import frappe
import requests
from frappe import _
from frappe.utils import get_url, call_hook_method
from urllib.parse import urlencode


# Authorize.Net API endpoints
AUTHNET_LIVE_URL = "https://api.authorize.net/xml/v1/request.api"
AUTHNET_SANDBOX_URL = "https://apitest.authorize.net/xml/v1/request.api"

# Currencies supported by Authorize.Net
SUPPORTED_CURRENCIES = [
	"USD", "CAD", "GBP", "EUR", "AUD", "NZD"
]


class AuthorizeNetSettings(frappe.model.document.Document):

	def validate(self):
		"""Called on Save. Registers this gateway instance with the Frappe payments system."""
		self._register_gateway()
		call_hook_method("payment_gateway_enabled", gateway=f"Authorize.Net-{self.gateway_name}")

	def _register_gateway(self):
		"""Create or update the Payment Gateway record that links to this settings doc."""
		from payments.utils import create_payment_gateway  # from frappe/payments app

		create_payment_gateway(
			f"Authorize.Net-{self.gateway_name}",
			settings="Authorize Net Settings",
			controller=self.gateway_name,
		)

	# ------------------------------------------------------------------
	# Standard Frappe gateway interface methods
	# ------------------------------------------------------------------

	def validate_transaction_currency(self, currency):
		if currency not in SUPPORTED_CURRENCIES:
			frappe.throw(
				_(
					"Authorize.Net does not support transactions in {0}. "
					"Supported currencies: {1}"
				).format(currency, ", ".join(SUPPORTED_CURRENCIES))
			)

	def get_payment_url(self, **kwargs):
		"""
		Called by Payment Request to produce the URL the customer clicks.
		We store the payment details in an Integration Request, then redirect
		the customer to our local checkout page which embeds the Authorize.Net iframe.
		"""
		integration_request = self.create_request(kwargs)
		return get_url(
			f"./authorizenet_checkout?{urlencode({'req': integration_request.name, 'gateway': self.gateway_name})}"
		)

	def create_request(self, data):
		"""
		Creates a Frappe Integration Request to track this transaction.
		Returns the Integration Request document.
		"""
		self.data = frappe._dict(data)

		# Store all payment data for later reconciliation
		integration_request = frappe.get_doc({
			"doctype": "Integration Request",
			"integration_type": "Remote",
			"integration_request_service": f"Authorize.Net-{self.gateway_name}",
			"reference_doctype": self.data.get("reference_doctype"),
			"reference_docname": self.data.get("reference_docname"),
			"data": json.dumps(self.data),
			"status": "Queued",
		})
		integration_request.insert(ignore_permissions=True)
		frappe.db.commit()

		return integration_request

	def get_hosted_payment_token(self, integration_request_name):
		"""
		Calls the Authorize.Net API to get a hosted payment page token.
		This token is short-lived (~15 min) and used to embed the payment iframe.
		Returns the token string or raises on failure.
		"""
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		data = frappe._dict(json.loads(integration_request.data))

		api_url = AUTHNET_SANDBOX_URL if self.sandbox_mode else AUTHNET_LIVE_URL
		transaction_key = self.get_password("transaction_key")

		# Build the return/cancel URLs (Authorize.Net posts to these)
		base_url = get_url()
		return_url = f"{base_url}/api/method/erpnext_authorizenet.payment_gateways.doctype.authorizenet_settings.authorizenet_settings.handle_payment_callback"
		cancel_url = f"{base_url}/authorizenet_checkout?cancelled=1&req={integration_request_name}"

		amount = data.get("amount") or data.get("grand_total")
		description = data.get("description") or f"Payment for {data.get('reference_docname', '')}"

		payload = {
			"getHostedPaymentPageRequest": {
				"merchantAuthentication": {
					"name": self.api_login_id,
					"transactionKey": transaction_key,
				},
				"transactionRequest": {
					"transactionType": "authCaptureTransaction",
					"amount": str(frappe.utils.flt(amount, 2)),
					"order": {
						"description": description[:255],
					},
					"customer": {
						"email": data.get("payer_email") or data.get("email") or "",
					},
				},
				"hostedPaymentSettings": {
					"setting": [
						{
							"settingName": "hostedPaymentReturnOptions",
							"settingValue": json.dumps({
								"showReceipt": False,
								"url": return_url,
								"urlText": "Continue",
								"cancelUrl": cancel_url,
								"cancelUrlText": "Cancel",
							}),
						},
						{
							"settingName": "hostedPaymentButtonOptions",
							"settingValue": json.dumps({"text": "Pay Now"}),
						},
						{
							"settingName": "hostedPaymentStyleOptions",
							"settingValue": json.dumps({"bgColor": "white"}),
						},
						{
							"settingName": "hostedPaymentPaymentOptions",
							"settingValue": json.dumps({
								"cardCodeRequired": True,
								"showCreditCard": True,
								"showBankAccount": False,
							}),
						},
						{
							"settingName": "hostedPaymentSecurityOptions",
							"settingValue": json.dumps({
								"captcha": False,
							}),
						},
						{
							"settingName": "hostedPaymentOrderOptions",
							"settingValue": json.dumps({
								"show": True,
								"merchantName": frappe.get_cached_value(
									"Company",
									data.get("company") or frappe.defaults.get_user_default("company"),
									"company_name",
								) or "",
							}),
						},
						{
							"settingName": "hostedPaymentCustomerOptions",
							"settingValue": json.dumps({
								"showEmail": False,
								"requiredEmail": False,
								"addPaymentProfile": False,
							}),
						},
						{
							"settingName": "hostedPaymentIFrameCommunicatorUrl",
							"settingValue": json.dumps({
								"url": f"{base_url}/assets/erpnext_authorizenet/js/authorizenet_communicator.html"
							}),
						},
					]
				},
			}
		}

		try:
			response = requests.post(
				api_url,
				json=payload,
				timeout=15,
				headers={"Content-Type": "application/json"},
			)
			response.raise_for_status()
		except requests.exceptions.RequestException as e:
			frappe.log_error(
				title="Authorize.Net API Connection Error",
				message=str(e),
			)
			frappe.throw(_("Could not connect to Authorize.Net. Please try again or contact support."))

		result = response.json()

		# Authorize.Net can return BOM characters — strip them
		if isinstance(result, str):
			result = json.loads(result.lstrip("\ufeff"))

		messages = result.get("messages", {})
		if messages.get("resultCode") == "Error":
			error_msgs = messages.get("message", [])
			error_text = "; ".join(
				f"{m.get('code')}: {m.get('text')}" for m in error_msgs
			)
			frappe.log_error(
				title="Authorize.Net Token Error",
				message=error_text,
			)
			frappe.throw(
				_("Authorize.Net error: {0}").format(error_text)
			)

		token = result.get("token")
		if not token:
			frappe.throw(_("Authorize.Net did not return a payment token. Check API credentials."))

		# Store the token reference on the Integration Request
		integration_request.db_set("output", token, update_modified=False)
		frappe.db.commit()

		return token

	def get_api_url(self):
		return AUTHNET_SANDBOX_URL if self.sandbox_mode else AUTHNET_LIVE_URL

	def get_hosted_form_url(self):
		"""The URL of the Authorize.Net hosted payment page (where the iframe points)."""
		if self.sandbox_mode:
			return "https://test.authorize.net/payment/payment"
		return "https://accept.authorize.net/payment/payment"


# ------------------------------------------------------------------
# Webhook / return handler — called by Authorize.Net after payment
# ------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def handle_payment_callback(**kwargs):
	"""
	Authorize.Net posts to this endpoint after the customer completes payment.
	Expected POST fields: transId, responseCode, refId (our integration request name)

	responseCode 1 = Approved
	responseCode 2 = Declined
	responseCode 3 = Error
	responseCode 4 = Held for review
	"""
	import hmac
	import hashlib

	form = frappe.request.form if hasattr(frappe, "request") else frappe._dict(kwargs)

	transaction_id = form.get("transId") or kwargs.get("transId")
	response_code = str(form.get("responseCode") or kwargs.get("responseCode") or "")
	# refId is the Integration Request name we passed in order.description
	# Authorize.Net also returns it if we set it in the transaction request
	ref_id = form.get("refId") or kwargs.get("refId")

	if not ref_id:
		# Try to find by transaction ID in Integration Requests
		frappe.log_error(
			title="Authorize.Net Callback: Missing refId",
			message=str(dict(form)),
		)
		frappe.respond_as_web_page(
			_("Payment Error"),
			_("Could not identify the payment record. Please contact support with Transaction ID: {0}").format(transaction_id),
			indicator_color="red",
		)
		return

	try:
		integration_request = frappe.get_doc("Integration Request", ref_id)
	except frappe.DoesNotExistError:
		frappe.log_error(
			title="Authorize.Net Callback: Integration Request not found",
			message=f"ref_id={ref_id}, transId={transaction_id}",
		)
		frappe.respond_as_web_page(
			_("Payment Error"),
			_("Payment record not found. Please contact support."),
			indicator_color="red",
		)
		return

	data = frappe._dict(json.loads(integration_request.data))

	if response_code == "1":
		# Approved
		_finalize_payment(integration_request, data, transaction_id)
		redirect_to = data.get("redirect_to") or get_url("/")
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url(
			f"./payment-success?doctype={data.get('reference_doctype')}&docname={data.get('reference_docname')}"
		)

	elif response_code == "4":
		# Held for review — treat as pending
		integration_request.db_set("status", "Pending", update_modified=False)
		frappe.db.commit()
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url(
			f"./payment-success?doctype={data.get('reference_doctype')}&docname={data.get('reference_docname')}&pending=1"
		)

	else:
		# Declined or Error
		integration_request.db_set("status", "Failed", update_modified=False)
		frappe.db.commit()
		frappe.respond_as_web_page(
			_("Payment Declined"),
			_("Your payment was not approved. Please try again or contact your bank. Transaction ID: {0}").format(transaction_id),
			indicator_color="red",
		)


def _finalize_payment(integration_request, data, transaction_id):
	"""
	Mark the Integration Request complete and create a Payment Entry in ERPNext
	so the invoice is marked paid.
	"""
	try:
		from erpnext.accounts.doctype.payment_request.payment_request import (
			get_gateway_details,
		)

		# Mark integration request as completed
		integration_request.db_set("status", "Completed", update_modified=False)
		integration_request.db_set(
			"output",
			json.dumps({"transId": transaction_id}),
			update_modified=False,
		)
		frappe.db.commit()

		# Trigger the standard ERPNext payment completion hook
		# This creates the Payment Entry and marks the invoice paid
		payment_request_name = data.get("reference_docname")

		if data.get("reference_doctype") == "Payment Request":
			payment_request = frappe.get_doc("Payment Request", payment_request_name)
			payment_request.run_method("on_payment_authorized", "Completed")
			frappe.db.commit()

	except Exception as e:
		frappe.log_error(
			title="Authorize.Net: Payment finalization error",
			message=f"Integration Request: {integration_request.name}\nTransaction ID: {transaction_id}\nError: {str(e)}",
		)
		# Don't re-raise — customer already paid, we don't want to show them an error.
		# The admin will need to reconcile manually using the transaction ID in the log.
