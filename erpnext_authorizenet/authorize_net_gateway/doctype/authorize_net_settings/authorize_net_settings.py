"""
authorize_net_settings.py

Controller for the Authorize Net Settings DocType.
Implements the standard Frappe payment gateway interface:
  - on_update()                -> registers the gateway after save
  - get_payment_url()          -> called by Payment Request to get the checkout URL
  - validate_transaction_currency()
  - create_request()           -> stores an Integration Request for this transaction
  - get_hosted_payment_token() -> calls Authorize.Net API for a hosted payment page token
  - handle_payment_callback()  -> processes the signed Webhook from Authorize.Net (failsafe)
  - handle_payment_return()    -> processes the redirect-back from Authorize.Net (primary)
  - handle_payment_cancel()    -> processes the cancel-redirect from Authorize.Net

Authorize.Net Accept Hosted flow (redirect-back primary, webhook failsafe):
  1. ERPNext calls get_payment_url() -> we store an Integration Request and return a checkout URL
  2. Customer lands on /authorizenet_checkout, which calls get_hosted_payment_token()
     The token request includes:
       - refId = Integration Request name (Authorize.Net stores as merchantReferenceId)
       - hostedPaymentReturnOptions with our return URL and cancel URL
  3. Customer pays on Authorize.Net's hosted form
  4. Authorize.Net POSTs back to /authnet_return with transId + our refId in the form body.
     handle_payment_return() finalizes the payment immediately and redirects the customer
     to our success page. This is the PRIMARY path.
  5. In parallel, Authorize.Net sends a signed Webhook to /authnet_webhook.
     handle_payment_callback() acts as a FAILSAFE for cases where the customer closed
     the browser before the redirect completed. It is idempotent — if the redirect path
     already finalized, it ack's and returns.
  6. If the customer clicks Cancel, AuthNet redirects to /authnet_cancel which marks the
     Integration Request as Cancelled and shows a friendly cancellation page.

Both success paths converge on _finalize_payment() which is idempotent via Integration
Request status check.
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

		Token request includes:
		  - refId = Integration Request name (truncated to 20 chars per AuthNet limit)
		  - hostedPaymentReturnOptions: AuthNet redirects here after payment (POST)
		"""
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		data = frappe._dict(json.loads(integration_request.data))

		api_url = AUTHNET_SANDBOX_URL if self.sandbox_mode else AUTHNET_LIVE_URL
		transaction_key = self.get_password("transaction_key")

		amount = data.get("amount") or data.get("grand_total")
		description = data.get("description") or f"Payment for {data.get('reference_docname', '')}"

		# Build return + cancel URLs — Authorize.Net redirects here after payment.
		# Both use clean paths (no dots) for consistency with the webhook setup.
		# IMPORTANT: keep cancel_url as a single short query param. AuthNet echoes
		# this URL into inline JS on their hosted page; complex query strings with
		# multiple `&`-joined params have been observed to break their HTML/JS
		# escaping and produce a malformed g_pageOptions block.
		return_url = get_url("/authnet_return")
		cancel_url = get_url(f"/authnet_cancel?req={integration_request_name}")

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
							# Suppress AuthNet's own receipt page — redirect straight back to us
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
		Calls Authorize.Net getTransactionDetailsRequest. Used ONLY by the webhook
		failsafe path to recover refId. The redirect-back primary path doesn't need
		this because refId comes back in the form POST.

		NOTE: Requires Transaction Details API to be enabled on the merchant account.
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


# ----------------------------------------------------------------------
# Redirect-back handler (PRIMARY success path)
# ----------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def handle_payment_return(**kwargs):
	"""
	Authorize.Net redirects the customer here after payment.

	When showReceipt=false with a return url, Authorize.Net POSTs the form body:
	  - transId         : Authorize.Net transaction ID
	  - refId           : echoed back from our token request (Integration Request name, truncated to 20 chars)
	  - response_code   : 1=approved, 2=declined, 3=error, 4=held for review
	  - response_reason_text
	  - amount, etc.

	We finalize the payment from this POST and then issue a redirect to our
	success/failure page. No external API call needed — refId is in the body.
	"""
	if not frappe.request:
		return _redirect_to_failure("invalid request")

	# AuthNet POSTs as application/x-www-form-urlencoded
	form = frappe.request.form if hasattr(frappe.request, "form") else {}
	if not form:
		# Fall back to query params (some configurations use GET)
		form = frappe.request.args if hasattr(frappe.request, "args") else {}
	if not form:
		form = kwargs

	transaction_id = (form.get("transId") or form.get("x_trans_id") or "").strip()
	ref_id = (form.get("refId") or form.get("x_ref_id") or "").strip()
	response_code = str(form.get("response_code") or form.get("x_response_code") or "").strip()

	if not transaction_id or not ref_id:
		frappe.log_error(
			title="Authorize.Net Return: missing transId or refId",
			message=f"form={dict(form)}",
		)
		return _redirect_to_failure("missing transaction reference")

	# response_code == "1" means approved. Anything else means declined/error.
	# AuthNet typically only redirects on success but be defensive.
	if response_code and response_code != "1":
		frappe.log_error(
			title="Authorize.Net Return: non-approved response",
			message=f"transId={transaction_id}, refId={ref_id}, code={response_code}, form={dict(form)}",
		)
		# Don't finalize — still acknowledge the redirect
		return _redirect_to_failure("payment not approved")

	# Match Integration Request — refId is truncated to 20 chars, so use prefix match
	integration_request = _match_integration_request(ref_id)

	if not integration_request:
		frappe.log_error(
			title="Authorize.Net Return: Integration Request not found",
			message=f"refId={ref_id}, transId={transaction_id}",
		)
		return _redirect_to_failure("payment record not found")

	data = frappe._dict(json.loads(integration_request.data))

	# Idempotency — if webhook beat us here, just redirect to success
	if integration_request.status == "Completed":
		return _redirect_to_success(data)

	# Finalize. Run as Administrator since this is a guest endpoint.
	frappe.set_user("Administrator")
	_finalize_payment(integration_request, data, transaction_id)

	return _redirect_to_success(data)


def _redirect_to_success(data):
	doctype = data.get("reference_doctype") or ""
	docname = data.get("reference_docname") or ""
	target = get_url(
		f"/authorizenet_return?{urlencode({'doctype': doctype, 'docname': docname})}"
	)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target
	return


def _redirect_to_failure(reason):
	target = get_url(f"/authorizenet_return?{urlencode({'failed': 1, 'reason': reason})}")
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target
	return


# ----------------------------------------------------------------------
# Cancel-redirect handler (cancellation path)
# ----------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def handle_payment_cancel(**kwargs):
	"""
	Authorize.Net redirects the customer here if they click Cancel on the
	hosted payment page.

	We mark the Integration Request as Cancelled (only if it's still pending —
	never clobber a Completed IR, since the webhook may have raced us in an
	edge case), then redirect to a friendly cancellation page.

	Single short query param `req=<integration_request_name>` — keep this
	minimal because AuthNet echoes the cancel URL into inline JS on their
	hosted page and complex URLs have been observed to break their escaping.
	"""
	ref_id = (
		(frappe.form_dict.get("req") if hasattr(frappe, "form_dict") else None)
		or kwargs.get("req")
		or ""
	).strip()

	if ref_id:
		integration_request = _match_integration_request(ref_id)
		if integration_request and integration_request.status in ("Queued", "Authorized"):
			frappe.set_user("Administrator")
			integration_request.db_set("status", "Cancelled", update_modified=False)
			frappe.db.commit()

	target = get_url("/authorizenet_return?cancelled=1")
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target
	return


# ----------------------------------------------------------------------
# Webhook handler (FAILSAFE path)
# ----------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def handle_payment_callback(**kwargs):
	"""
	Receives a signed Authorize.Net Webhook (POST with JSON body).

	Acts as a failsafe for the case where the customer closed the browser
	before the redirect-back completed. Idempotent — if the redirect already
	finalized, the IR status check below ack's and returns.

	Headers:
	  X-ANET-Signature: sha512=HEX  -> HMAC-SHA512 of raw body, keyed by signature_key
	"""
	# Read raw body BEFORE Frappe parses it, since signature is over raw bytes
	raw_body = frappe.request.get_data() if hasattr(frappe, "request") else b""

	if not raw_body:
		frappe.local.response["http_status_code"] = 400
		return {"error": "empty body"}

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

	if event_type not in PAYMENT_SUCCESS_EVENTS:
		frappe.logger().info(
			f"Authorize.Net webhook received but ignored: event={event_type}, transId={transaction_id}"
		)
		return {"status": "ignored", "event": event_type}

	# Recover refId via getTransactionDetails — this requires the Transaction
	# Details API permission on the merchant account. Only used as failsafe.
	tx_details = settings.fetch_transaction_details(transaction_id)
	if not tx_details:
		frappe.log_error(
			title="Authorize.Net Webhook: could not fetch transaction details",
			message=f"transId={transaction_id}",
		)
		frappe.local.response["http_status_code"] = 500
		return {"error": "transaction lookup failed"}

	ref_id = (
		tx_details.get("refId") or
		tx_details.get("refTransId") or
		tx_details.get("order", {}).get("invoiceNumber") or
		""
	)

	if not ref_id:
		frappe.log_error(
			title="Authorize.Net Webhook: no refId on transaction",
			message=f"transId={transaction_id}, transaction={json.dumps(tx_details)[:2000]}",
		)
		frappe.local.response["http_status_code"] = 200
		return {"status": "no refId"}

	integration_request = _match_integration_request(ref_id)

	if not integration_request:
		frappe.log_error(
			title="Authorize.Net Webhook: Integration Request not found",
			message=f"refId={ref_id}, transId={transaction_id}",
		)
		frappe.local.response["http_status_code"] = 200
		return {"status": "integration request not found"}

	# Idempotency: if redirect-back path already finalized, ack and return
	if integration_request.status == "Completed":
		return {"status": "already completed"}

	data = frappe._dict(json.loads(integration_request.data))
	_finalize_payment(integration_request, data, transaction_id)

	return {"status": "ok"}


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def _match_integration_request(ref_id):
	"""
	Match an Integration Request by refId. Tries exact match first, then
	prefix match (since refId is truncated to 20 chars by Authorize.Net).
	"""
	if not ref_id:
		return None

	try:
		return frappe.get_doc("Integration Request", ref_id)
	except frappe.DoesNotExistError:
		pass

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
		return frappe.get_doc("Integration Request", matches[0])

	return None


def _verify_and_get_settings(raw_body, signature_header):
	"""
	Try each Authorize Net Settings record's signature_key against the body.
	Returns the matching settings doc, or None.
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

		try:
			key_bytes = bytes.fromhex(signature_key)
		except ValueError:
			key_bytes = signature_key.encode("utf-8")

		computed = hmac.new(key_bytes, raw_body, hashlib.sha512).hexdigest().lower()

		if hmac.compare_digest(computed, provided_hex):
			return settings

	return None


def _finalize_payment(integration_request, data, transaction_id):
	"""
	Convergence point for both redirect and webhook paths.
	Marks the Integration Request complete, then calls on_payment_authorized
	on the Payment Request to create and submit the Payment Entry.

	Idempotency is enforced by the caller checking IR status before invoking.
	"""
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
			# on_payment_authorized handles status transition + Payment Entry creation
			# in one go, with all the right hooks. Matches the NMI handler pattern.
			payment_request.run_method("on_payment_authorized", "Completed")
			frappe.db.commit()

	except Exception as e:
		frappe.log_error(
			title="Authorize.Net: Payment finalization error",
			message=f"Integration Request: {integration_request.name}\nTransaction ID: {transaction_id}\nError: {str(e)}\nTraceback:\n{traceback.format_exc()}",
		)
