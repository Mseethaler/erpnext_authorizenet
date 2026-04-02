"""
authorize_net_settings.py

Controller for the Authorize Net Settings DocType.
Implements the standard Frappe payment gateway interface:
  - on_update()                -> registers the gateway after save
  - get_payment_url()          -> called by Payment Request to get the checkout URL
  - validate_transaction_currency()
  - create_request()           -> stores an Integration Request for this transaction
  - get_hosted_payment_token() -> calls Authorize.Net API for a hosted payment page token
  - handle_payment_callback()  -> processes the Silent Post from Authorize.Net

Authorize.Net Accept Hosted flow:
  1. ERPNext calls get_payment_url() -> we store an Integration Request and return a checkout URL
  2. Customer lands on /authorizenet_checkout, which calls get_hosted_payment_token()
  3. Our checkout page POSTs the token to Authorize.Net's hosted form inside an iframe
  4. Authorize.Net Silent Posts result to handle_payment_callback (server-to-server)
  5. We match the transaction to an Integration Request by scanning the data JSON description
  6. We create a Payment Entry and mark the invoice paid
"""

import json
import traceback
import frappe
import requests
from frappe import _
from frappe.utils import get_url, call_hook_method
from urllib.parse import urlencode


AUTHNET_LIVE_URL = "https://api.authorize.net/xml/v1/request.api"
AUTHNET_SANDBOX_URL = "https://apitest.authorize.net/xml/v1/request.api"

SUPPORTED_CURRENCIES = ["USD", "CAD", "GBP", "EUR", "AUD", "NZD"]


class AuthorizeNetSettings(frappe.model.document.Document):

	def on_update(self):
		"""Called after Save. Registers this gateway with the Frappe payments system."""
		self._register_gateway()
		call_hook_method("payment_gateway_enabled", gateway=f"Authorize.Net-{self.gateway_name}")

	def _register_gateway(self):
		from payments.utils import create_payment_gateway
		create_payment_gateway(
			f"Authorize.Net-{self.gateway_name}",
			settings="Authorize Net Settings",
			controller=self.gateway_name,
		)

	def validate_transaction_currency(self, currency):
		if currency not in SUPPORTED_CURRENCIES:
			frappe.throw(
				_("Authorize.Net does not support transactions in {0}. Supported: {1}").format(
					currency, ", ".join(SUPPORTED_CURRENCIES)
				)
			)

	def get_payment_url(self, **kwargs):
		integration_request = self.create_request(kwargs)
		return get_url(
			f"./authorizenet_checkout?{urlencode({'req': integration_request.name, 'gateway': self.gateway_name})}"
		)

	def create_request(self, data):
		self.data = frappe._dict(data)
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
		Token is short-lived (~15 min) and used to POST to the hosted payment iframe.
		"""
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		data = frappe._dict(json.loads(integration_request.data))

		api_url = AUTHNET_SANDBOX_URL if self.sandbox_mode else AUTHNET_LIVE_URL
		transaction_key = self.get_password("transaction_key")
		base_url = get_url()

		return_url = (
			f"{base_url}/api/method/"
			"erpnext_authorizenet.authorize_net_gateway.doctype"
			".authorize_net_settings.authorize_net_settings.handle_payment_callback"
		)

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
							"settingValue": json.dumps({"captcha": False}),
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
			frappe.log_error(title="Authorize.Net API Connection Error", message=str(e))
			frappe.throw(_("Could not connect to Authorize.Net. Please try again or contact support."))

		result = json.loads(response.content.decode("utf-8-sig"))

		messages = result.get("messages", {})
		if messages.get("resultCode") == "Error":
			error_msgs = messages.get("message", [])
			error_text = "; ".join(f"{m.get('code')}: {m.get('text')}" for m in error_msgs)
			frappe.log_error(title="Authorize.Net Token Error", message=error_text)
			frappe.throw(_("Authorize.Net error: {0}").format(error_text))

		token = result.get("token")
		if not token:
			frappe.throw(_("Authorize.Net did not return a payment token. Check API credentials."))

		integration_request.db_set("output", token, update_modified=False)
		frappe.db.commit()
		return token

	def get_api_url(self):
		return AUTHNET_SANDBOX_URL if self.sandbox_mode else AUTHNET_LIVE_URL

	def get_hosted_form_url(self):
		if self.sandbox_mode:
			return "https://test.authorize.net/payment/payment"
		return "https://accept.authorize.net/payment/payment"


@frappe.whitelist(allow_guest=True)
def handle_payment_callback(**kwargs):
	"""
	Handles both:
	1. Authorize.Net Silent Post (server-to-server) -- uses x_ prefixed fields
	2. Authorize.Net hosted page redirect -- uses camelCase fields

	Silent Post fields: x_response_code, x_trans_id, x_description
	Redirect fields: responseCode, transId, refId
	"""
	form = frappe.request.form if hasattr(frappe, "request") else frappe._dict(kwargs)

	# Log the full form data for debugging
	frappe.log_error(
		title="Authorize.Net Callback Received",
		message=str(dict(form)),
	)

	# Support both Silent Post (x_ prefix) and hosted page redirect fields
	transaction_id = (
		form.get("x_trans_id") or
		form.get("transId") or
		kwargs.get("transId") or ""
	)
	response_code = str(
		form.get("x_response_code") or
		form.get("responseCode") or
		kwargs.get("responseCode") or ""
	)

	# Try to find Integration Request via refId first
	ref_id = form.get("refId") or kwargs.get("refId")
	integration_request = None

	if ref_id:
		try:
			integration_request = frappe.get_doc("Integration Request", ref_id)
		except frappe.DoesNotExistError:
			pass

	if not integration_request:
		# Fall back: scan recent Queued Authorize.Net Integration Requests
		# and match by the description stored in their data JSON field
		description = form.get("x_description") or ""

		if description:
			candidates = frappe.get_all(
				"Integration Request",
				filters={
					"status": "Queued",
					"integration_request_service": ["like", "Authorize.Net-%"],
				},
				fields=["name", "data"],
				order_by="creation desc",
				limit=20,
			)
			for candidate in candidates:
				try:
					candidate_data = json.loads(candidate.data or "{}")
					if candidate_data.get("description") == description:
						integration_request = frappe.get_doc(
							"Integration Request", candidate.name
						)
						break
				except Exception:
					continue

	if not integration_request:
		frappe.log_error(
			title="Authorize.Net Callback: Integration Request not found",
			message=f"transId={transaction_id}, description={form.get('x_description')}, form={dict(form)}",
		)
		return

	data = frappe._dict(json.loads(integration_request.data))

	if response_code == "1":
		_finalize_payment(integration_request, data, transaction_id)
		# Only redirect for hosted page callbacks, not Silent Post
		if form.get("refId") or kwargs.get("refId"):
			frappe.local.response["type"] = "redirect"
			frappe.local.response["location"] = get_url(
				f"./payment-success?doctype={data.get('reference_doctype')}&docname={data.get('reference_docname')}"
			)

	elif response_code == "4":
		integration_request.db_set("status", "Pending", update_modified=False)
		frappe.db.commit()

	else:
		integration_request.db_set("status", "Failed", update_modified=False)
		frappe.db.commit()


def _finalize_payment(integration_request, data, transaction_id):
	try:
		integration_request.db_set("status", "Completed", update_modified=False)
		integration_request.db_set(
			"output",
			json.dumps({"transId": transaction_id}),
			update_modified=False,
		)
		frappe.db.commit()

		if data.get("reference_doctype") == "Payment Request":
			payment_request = frappe.get_doc(
				"Payment Request", data.get("reference_docname")
			)
			# Mark Payment Request as paid
			payment_request.db_set("status", "Paid", update_modified=False)
			frappe.db.commit()
			# Create and submit the Payment Entry
			# Callback runs as guest so we need elevated permissions
			frappe.set_user("Administrator")
			payment_request.create_payment_entry(submit=True)
			frappe.db.commit()

	except Exception as e:
		frappe.log_error(
			title="Authorize.Net: Payment finalization error",
			message=f"Integration Request: {integration_request.name}\nTransaction ID: {transaction_id}\nError: {str(e)}\nTraceback:\n{traceback.format_exc()}",
		)
