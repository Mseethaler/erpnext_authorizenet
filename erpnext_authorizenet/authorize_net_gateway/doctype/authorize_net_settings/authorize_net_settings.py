"""
authorize_net_settings.py

Controller for the Authorize Net Settings DocType.
Implements the standard Frappe payment gateway interface:
  - on_update()                -> registers the gateway after save
  - get_payment_url()          -> called by Payment Request to get the checkout URL
  - validate_transaction_currency()
  - create_request()           -> stores an Integration Request for this transaction
  - get_hosted_payment_token() -> calls Authorize.Net API for a hosted payment page token
  - handle_payment_callback()  -> processes the signed Webhook from Authorize.Net

Authorize.Net Accept Hosted flow (Webhook variant):
  1. ERPNext calls get_payment_url() -> we store an Integration Request and return a checkout URL
  2. Customer lands on /authorizenet_checkout, which calls get_hosted_payment_token()
     The token request includes refId = Integration Request name, which Authorize.Net
     stores against the transaction as merchantReferenceId.
  3. Our checkout page POSTs the token to Authorize.Net's hosted form
  4. Authorize.Net processes payment and shows their receipt page
  5. Authorize.Net sends a signed JSON Webhook (HMAC-SHA512) to handle_payment_callback
  6. We verify the signature, fetch full transaction details (which includes refId),
     match to our Integration Request, and create a Payment Entry

Webhook docs: https://developer.authorize.net/api/reference/features/webhooks.html
"""

import hashlib
import hmac
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

# Webhook event types we care about
EVENT_AUTH_CAPTURE_CREATED = "net.authorize.payment.authcapture.created"
EVENT_CAPTURE_CREATED = "net.authorize.payment.capture.created"
EVENT_AUTH_CREATED = "net.authorize.payment.authorization.created"
EVENT_VOID_CREATED = "net.authorize.payment.void.created"
EVENT_REFUND_CREATED = "net.authorize.payment.refund.created"

PAYMENT_SUCCESS_EVENTS = {
	EVENT_AUTH_CAPTURE_CREATED,
	EVENT_CAPTURE_CREATED,
}


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
		Token is short-lived (~15 min) and used to POST to the hosted payment page.

		IMPORTANT: refId is set to the Integration Request name, which Authorize.Net
		stores against the transaction as merchantReferenceId. The webhook handler
		uses this to match callbacks back to ERPNext records.
		"""
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		data = frappe._dict(json.loads(integration_request.data))

		api_url = AUTHNET_SANDBOX_URL if self.sandbox_mode else AUTHNET_LIVE_URL
		transaction_key = self.get_password("transaction_key")

		amount = data.get("amount") or data.get("grand_total")
		description = data.get("description") or f"Payment for {data.get('reference_docname', '')}"

		payload = {
			"getHostedPaymentPageRequest": {
				"merchantAuthentication": {
					"name": self.api_login_id,
					"transactionKey": transaction_key,
				},
				"refId": integration_request_name[:20],  # Authorize.Net caps refId at 20 chars
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
								"showReceipt": True,
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

	def fetch_transaction_details(self, transaction_id):
		"""
		Calls Authorize.Net getTransactionDetailsRequest to retrieve the full
		transaction record, including refId (merchantReferenceId), amount,
		response code, and order description.

		Webhook payloads only include id and entityName, not refId, so we
		must look up the full record to match it to our Integration Request.
		"""
		api_url = self.get_api_url()
		transaction_key = self.get_password("transaction_key")

		payload = {
			"getTransactionDetailsRequest": {
				"merchantAuthentication": {
					"name": self.api_login_id,
					"transactionKey": transaction_key,
				},
				"transId": str(transaction_id),
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
				title="Authorize.Net getTransactionDetails Connection Error",
				message=str(e),
			)
			return None

		try:
			result = json.loads(response.content.decode("utf-8-sig"))
		except json.JSONDecodeError as e:
			frappe.log_error(
				title="Authorize.Net getTransactionDetails Parse Error",
				message=f"Body: {response.text}\nError: {e}",
			)
			return None

		messages = result.get("messages", {})
		if messages.get("resultCode") == "Error":
			error_msgs = messages.get("message", [])
			error_text = "; ".join(f"{m.get('code')}: {m.get('text')}" for m in error_msgs)
			frappe.log_error(
				title="Authorize.Net getTransactionDetails Error",
				message=f"transId={transaction_id}: {error_text}",
			)
			return None

		return result.get("transaction")


@frappe.whitelist(allow_guest=True)
def handle_payment_callback(**kwargs):
	"""
	Receives a signed Authorize.Net Webhook (POST with JSON body).

	Headers:
	  X-ANET-Signature: sha512=HEX  -> HMAC-SHA512 of raw body, keyed by signature_key

	Body (JSON), example for net.authorize.payment.authcapture.created:
	  {
	    "notificationId": "uuid",
	    "eventType": "net.authorize.payment.authcapture.created",
	    "eventDate": "2026-04-30T...",
	    "webhookId": "uuid",
	    "payload": {
	      "responseCode": 1,
	      "authCode": "...",
	      "avsResponse": "Y",
	      "authAmount": 12.34,
	      "merchantReferenceId": "<NOT in payload — must fetch via API>",
	      "id": "<transaction id>",
	      "entityName": "transaction"
	    }
	  }

	Note: the webhook payload itself does NOT include merchantReferenceId,
	despite some older docs. We must call getTransactionDetailsRequest with
	the transaction id to retrieve refId.
	"""
	# Read raw body BEFORE Frappe parses it, since signature is over raw bytes
	raw_body = frappe.request.get_data() if hasattr(frappe, "request") else b""

	if not raw_body:
		frappe.local.response["http_status_code"] = 400
		return {"error": "empty body"}

	# Parse JSON
	try:
		body = json.loads(raw_body)
	except json.JSONDecodeError:
		frappe.log_error(
			title="Authorize.Net Webhook: invalid JSON",
			message=raw_body[:2000],
		)
		frappe.local.response["http_status_code"] = 400
		return {"error": "invalid json"}

	event_type = body.get("eventType", "")
	payload = body.get("payload", {}) or {}
	transaction_id = str(payload.get("id") or "")

	if not transaction_id:
		frappe.log_error(
			title="Authorize.Net Webhook: missing transaction id",
			message=json.dumps(body)[:2000],
		)
		frappe.local.response["http_status_code"] = 400
		return {"error": "missing transaction id"}

	# Find the right gateway settings doc by trying each one until signature verifies.
	# (Multiple gateways may share an account, but typically there's only one.)
	signature_header = (
		frappe.get_request_header("X-ANET-Signature") or
		frappe.get_request_header("x-anet-signature") or
		""
	)

	if not signature_header:
		frappe.log_error(
			title="Authorize.Net Webhook: missing signature header",
			message=f"event={event_type}, transId={transaction_id}",
		)
		frappe.local.response["http_status_code"] = 401
		return {"error": "missing signature"}

	settings = _verify_and_get_settings(raw_body, signature_header)
	if not settings:
		frappe.log_error(
			title="Authorize.Net Webhook: signature verification failed",
			message=f"event={event_type}, transId={transaction_id}",
		)
		frappe.local.response["http_status_code"] = 401
		return {"error": "signature verification failed"}

	# Only act on payment success events. Other events (refunds, voids) can be
	# wired up later — log them for now so they're visible.
	if event_type not in PAYMENT_SUCCESS_EVENTS:
		frappe.logger().info(
			f"Authorize.Net webhook received but ignored: event={event_type}, transId={transaction_id}"
		)
		return {"status": "ignored", "event": event_type}

	# Look up full transaction to get refId (merchantReferenceId)
	tx_details = settings.fetch_transaction_details(transaction_id)
	if not tx_details:
		frappe.log_error(
			title="Authorize.Net Webhook: could not fetch transaction details",
			message=f"transId={transaction_id}",
		)
		frappe.local.response["http_status_code"] = 500
		return {"error": "transaction lookup failed"}

	ref_id = tx_details.get("refTransId") or tx_details.get("order", {}).get("invoiceNumber") or ""
	# refId from the original request comes back as the top-level "refTransId" on
	# the response in some cases, but the canonical location is the order block's
	# invoiceNumber when set, OR — for getTransactionDetails responses — the
	# top-level "refId" of the transaction. Try several known locations.
	ref_id = (
		tx_details.get("refId") or
		tx_details.get("refTransId") or
		ref_id or
		""
	)

	if not ref_id:
		frappe.log_error(
			title="Authorize.Net Webhook: no refId on transaction",
			message=f"transId={transaction_id}, transaction={json.dumps(tx_details)[:2000]}",
		)
		frappe.local.response["http_status_code"] = 200
		return {"status": "no refId"}

	# Match Integration Request — refId may be truncated to 20 chars, so we use
	# 'starts with' match if needed.
	integration_request = None
	try:
		integration_request = frappe.get_doc("Integration Request", ref_id)
	except frappe.DoesNotExistError:
		# Try prefix match (refId was truncated)
		matches = frappe.get_all(
			"Integration Request",
			filters={
				"name": ["like", f"{ref_id}%"],
				"integration_request_service": ["like", "Authorize.Net-%"],
			},
			pluck="name",
			limit=1,
		)
		if matches:
			integration_request = frappe.get_doc("Integration Request", matches[0])

	if not integration_request:
		frappe.log_error(
			title="Authorize.Net Webhook: Integration Request not found",
			message=f"refId={ref_id}, transId={transaction_id}",
		)
		frappe.local.response["http_status_code"] = 200
		return {"status": "integration request not found"}

	# Idempotency: if already completed, ack and return
	if integration_request.status == "Completed":
		return {"status": "already completed"}

	data = frappe._dict(json.loads(integration_request.data))
	_finalize_payment(integration_request, data, transaction_id)

	return {"status": "ok"}


def _verify_and_get_settings(raw_body, signature_header):
	"""
	Try each Authorize Net Settings record's signature_key against the body.
	Returns the matching settings doc, or None.

	signature_header format: 'sha512=HEXDIGEST'
	"""
	if "=" in signature_header:
		_, _, provided_hex = signature_header.partition("=")
	else:
		provided_hex = signature_header
	provided_hex = provided_hex.strip().lower()

	if not provided_hex:
		return None

	settings_names = frappe.get_all("Authorize Net Settings", pluck="name")

	for name in settings_names:
		settings = frappe.get_doc("Authorize Net Settings", name)
		signature_key = settings.get_password("signature_key", raise_exception=False)
		if not signature_key:
			continue

		# Authorize.Net signature key is stored as ASCII hex. The HMAC key is
		# the hex string decoded to bytes.
		try:
			key_bytes = bytes.fromhex(signature_key)
		except ValueError:
			# If the key isn't hex, fall back to using it as-is (some merchants
			# paste it in raw form).
			key_bytes = signature_key.encode("utf-8")

		computed = hmac.new(key_bytes, raw_body, hashlib.sha512).hexdigest().lower()

		if hmac.compare_digest(computed, provided_hex):
			return settings

	return None


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
			# Webhook runs as guest so we need elevated permissions
			frappe.set_user("Administrator")
			payment_request.create_payment_entry(submit=True)
			frappe.db.commit()

	except Exception as e:
		frappe.log_error(
			title="Authorize.Net: Payment finalization error",
			message=f"Integration Request: {integration_request.name}\nTransaction ID: {transaction_id}\nError: {str(e)}\nTraceback:\n{traceback.format_exc()}",
		)
