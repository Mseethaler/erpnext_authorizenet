# ERPNext Authorize.Net & NMI Payment Gateway

A Frappe/ERPNext app that adds Authorize.Net (and NMI) payment gateway support, for businesses that cannot use Stripe, PayPal, or Square — including firearms dealers, outdoor retailers, and other 2A-adjacent trades.

Built by [Digital Sovereignty](https://digital-sovereignty.cc) as a drop-in companion to the `payments` app.

## Gateways Included

- **Authorize.Net** (Accept Hosted — PCI-compliant hosted payment form, redirect-back primary, signed webhook failsafe)
- **NMI** (Network Merchants Inc.) — *stub ready, activate when credentials are available*

## Installation

```bash
# From your bench directory
bench get-app https://github.com/Mseethaler/erpnext_authorizenet
bench --site <yoursite> install-app erpnext_authorizenet
bench --site <yoursite> migrate
```

## Reverse Proxy Configuration

Authorize.Net's URL validators reject URLs containing dots in path segments. Frappe's standard API method URL contains dots (`/api/method/some.module.function`), so you need to expose clean paths via your reverse proxy.

Add these three blocks to your nginx server block for the ERPNext site:

```nginx
# Authorize.Net signed webhook (failsafe payment recording)
location = /authnet_webhook {
    rewrite ^.*$ /api/method/erpnext_authorizenet.api.authnet_webhook break;
    proxy_pass http://your-frappe-upstream;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
}

# Authorize.Net hosted-form redirect-back (primary payment recording)
location = /authnet_return {
    rewrite ^.*$ /api/method/erpnext_authorizenet.api.authnet_return break;
    proxy_pass http://your-frappe-upstream;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
}

# Authorize.Net cancel-redirect (when customer clicks Cancel on hosted form)
location = /authnet_cancel {
    rewrite ^.*$ /api/method/erpnext_authorizenet.api.authnet_cancel break;
    proxy_pass http://your-frappe-upstream;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
}
```

Replace `http://your-frappe-upstream` with whatever your existing `location /` block proxies to.

After adding, validate and reload:

```bash
nginx -t && nginx -s reload
```

## Configuration

### 1. Generate a Signature Key in Authorize.Net

Required before creating any webhook.

- Account → Settings → API Credentials & Keys
- Select "New Signature Key", check "Disable Old Signature Key", Submit
- Copy the hex string — it's only shown once

### 2. Enable Transaction Details API

Required for the webhook failsafe path.

- Account → Settings → Security Settings → General Security Settings
- Enable "Transaction Details API"

Without this, the webhook handler cannot recover the transaction's refId and will fail with E00007. The redirect-back path doesn't need this, but the failsafe does.

### 3. Create the Webhook in Authorize.Net

- Account → Settings → Webhook Notifications → Add Endpoint
- **Webhook notification name**: any descriptive label (e.g. "ERPNext")
- **Endpoint URL**: `https://<your-domain>/authnet_webhook`
- **Status**: Active
- **Events** — subscribe to:
  - `net.authorize.payment.authcapture.created` (required)
  - `net.authorize.payment.capture.created` (also handled, for split auth+capture flows)
- Save. Authorize.Net will issue a save-time probe to your endpoint; the handler returns 200 to any request without a signature header so the save will succeed.

> **Double-check the URL after saving.** Authorize.Net's UI does not validate that the host resolves. A typo or wrong domain will silently swallow every webhook delivery without warning.

### 4. (Optional but recommended) Whitelist Response/Receipt URLs

For the redirect-back primary path to work, your return and cancel URLs may need to be whitelisted in Authorize.Net's "Response/Receipt URLs" allowlist. Without this, Authorize.Net will silently ignore `hostedPaymentReturnOptions` and show its own receipt page after payment — leaving the webhook failsafe to do the work.

In the new Authorize.Net UI, this setting is hard to find. If you can't locate it, toggle off "New Authorize.net" at the top of the page and look in the classic UI under Account → Settings → Transaction Format Settings → "Response/Receipt URLs".

Add:

- `https://<your-domain>/authnet_return`
- `https://<your-domain>/authnet_cancel`

If you cannot enable this whitelist, the integration will still work — the webhook failsafe handles all payment recording. You will just lose the immediate post-payment redirect to your own confirmation page, and customers will see Authorize.Net's receipt page instead.

### 5. Configure ERPNext

- Open **Authorize Net Settings**
- Enter **API Login ID** and **Transaction Key** from your Authorize.Net merchant account
- Enter the **Signature Key** from step 1
- Set **Sandbox Mode** appropriately
- Save — the Payment Gateway is registered automatically

### 6. Create a Payment Gateway Account

- List view → Payment Gateway Account → New
- Payment Gateway: `Authorize.Net-<your gateway name>`
- Payment Account: your bank or undeposited funds GL account
- Save

## How It Works

1. ERPNext generates a Sales Invoice
2. A Payment Request is created, pointing to the Authorize.Net gateway
3. Customer clicks the payment link → ERPNext fetches a hosted payment token from Authorize.Net. The token request includes:
   - `refId` (Integration Request name, truncated to 20 chars per Authorize.Net's limit)
   - `order.invoiceNumber` (also the IR name — used by the webhook failsafe to recover the IR if needed)
   - `hostedPaymentReturnOptions` with our return and cancel URLs and `showReceipt: false`
4. Customer is redirected to Authorize.Net's secure hosted form to enter card details
5. **Primary path**: After payment, Authorize.Net POSTs the customer back to `/authnet_return` with `transId` and our `refId` in the form body. ERPNext finalizes the Payment Entry on the spot and redirects the customer to the confirmation page.
6. **Failsafe path**: Authorize.Net also sends a signed (HMAC-SHA512) webhook to `/authnet_webhook`. If the customer closed the browser before step 5 completed (or if redirect-back is suppressed for any reason), the webhook recovers the IR by calling `getTransactionDetailsRequest` and matching on `order.invoiceNumber`, then creates the Payment Entry.

Both paths converge on a shared `_finalize_payment` function and are idempotent — whichever fires first wins, the other ack's and exits.

## Implementation Notes & Gotchas

These are non-obvious behaviors discovered during development. They're documented here so the next maintainer doesn't have to re-derive them.

### Webhook signature uses the signature key as a UTF-8 string, not as decoded hex bytes

Authorize.Net's webhook signature is HMAC-SHA512 of the raw request body. The key is a 128-character hex string in the merchant portal — it looks like it should be decoded to 64 raw bytes before use, but **Authorize.Net signs using the key as a UTF-8 string of hex characters**. Decoding the hex first will produce a wrong HMAC and every signature will fail verification. This integration uses `signature_key.encode("utf-8")` directly.

### `order.invoiceNumber` is required for the webhook failsafe

Authorize.Net's `getTransactionDetailsRequest` does not return the `refId` we sent in the original token request. To recover the Integration Request when only a `transId` is known (the webhook case), we set `order.invoiceNumber` to the IR name in the original token request. `getTransactionDetailsRequest` echoes it back, and we match on that. Without this, the webhook handler cannot identify which Integration Request a webhook corresponds to.

### `cancelUrl` must be a single short query param

Authorize.Net injects `hostedPaymentReturnOptions` JSON into inline JavaScript on their hosted page. Complex URLs with multiple `&`-joined query params have been observed to break their HTML/JS escaping, producing a malformed `g_pageOptions` block and a blank checkout page. Keep `cancelUrl` minimal — `?req=<integration_request_name>` only.

### `showReceipt: false` is not always honored

Authorize.Net may silently ignore `showReceipt: false` and display its own receipt page if the return URL isn't on the merchant account's "Response/Receipt URLs" whitelist (see Configuration step 4). When this happens, the redirect-back primary path is bypassed entirely, and the webhook failsafe handles all payment recording. The integration remains functional, but the post-payment UX is suboptimal.

### Stock ERPNext `Payment Request` does not implement `on_payment_authorized`

Some payment gateway integrations (Razorpay, Paynow, etc.) call `ref_doc.run_method("on_payment_authorized", "Completed")` to trigger Payment Entry creation. This works when the reference doctype is a custom doctype that implements the hook, or when an app subclasses Payment Request to add it. **Stock Payment Request does not implement this method**, so `run_method` is a silent no-op and no Payment Entry is created. This integration calls `payment_request.create_payment_entry()` directly instead.

### Payment Request status must be `Requested` for `create_payment_entry` to proceed

When `get_payment_url` is called (i.e. when the customer first visits the payment link), the Payment Request status transitions from `Initiated` to whatever the gateway sets. By the time the webhook fires, the status may still be `Initiated` rather than `Requested`. `create_payment_entry` silently bails if status is anything other than `Requested`. The handler explicitly bumps `Initiated`/`Draft` → `Requested` before calling `create_payment_entry`.

### Webhook handler must run as Administrator

The webhook arrives as Guest. `Payment Request.create_payment_entry()` requires permissions Guest doesn't have (specifically: ability to submit a Payment Entry against the reference doctype). The handler calls `frappe.set_user("Administrator")` before invoking `_finalize_payment`. The redirect-back handler does the same.

### Don't track `version-15` in production — pin to a tag or commit SHA

ERPNext's `version-15` branch ships breaking changes between point releases. During development of this integration, ERPNext v15.107.0 introduced a JS call to `make_payment_request_with_schedule` that has no corresponding function definition — every "Create → Payment Request" click silently fails. v15.106.0 does not have this bug. Pin your `apps.json` to a known-good tag or commit SHA, not a moving branch.

## Repository Layout

```
erpnext_authorizenet/
├── apps.json
├── erpnext_authorizenet/
│   ├── api.py                                    # Public webhook + return + cancel endpoints (thin routers)
│   ├── authorize_net_gateway/
│   │   └── doctype/
│   │       ├── authorize_net_settings/
│   │       │   ├── authorize_net_settings.json   # DocType definition
│   │       │   └── authorize_net_settings.py     # Full controller — gateway interface,
│   │       │                                     # token request, return handler, cancel
│   │       │                                     # handler, webhook handler, finalization
│   │       └── nmi_settings/                     # Stub gateway (activate when credentials available)
│   ├── hooks.py
│   ├── install.py
│   ├── modules.txt                               # Lists "Authorize Net Gateway"
│   └── templates/pages/
│       ├── authorizenet_checkout.{html,py}       # Customer checkout page (calls token API)
│       ├── authorizenet_return.{html,py}         # Confirmation page (post-payment)
│       └── nmi_checkout.{html,py}
├── README.md
└── setup.py
```

> **Warning:** `authorize_net_settings.py` is a ~600-line controller file. Do not confuse it with `api.py` (a ~80-line thin router). Replacing the controller's contents with the router's will leave the `AuthorizeNetSettings` class undefined; the next `bench migrate` will then mark the DocType as orphaned and **delete it from the database** during `remove_orphan_doctypes()`. If this happens, restore the controller and re-migrate.

## apps.json entry

```json
{
  "url": "https://github.com/Mseethaler/erpnext_authorizenet",
  "branch": "main"
}
```

For production deployments, pin to a known-good commit SHA instead of a branch. ERPNext should also be pinned to a specific tag.

## License

MIT
