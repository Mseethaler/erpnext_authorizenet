# ERPNext Authorize.Net & NMI Payment Gateway

A Frappe/ERPNext app that adds Authorize.Net (and NMI) payment gateway support, for businesses that cannot use Stripe, PayPal, or Square — including firearms dealers, outdoor retailers, and other 2A-adjacent trades.

Built by [Digital Sovereignty](https://digital-sovereignty.cc) as a drop-in companion to the `payments` app.

## Gateways Included

- **Authorize.Net** (Accept Hosted — PCI-compliant hosted payment form, signed webhook callbacks)
- **NMI** (Network Merchants Inc.) — *stub ready, activate when credentials are available*

## Installation

```bash
# From your bench directory
bench get-app https://github.com/Mseethaler/erpnext_authorizenet
bench --site <yoursite> install-app erpnext_authorizenet
bench --site <yoursite> migrate
```

## Reverse Proxy Configuration

Authorize.Net's webhook URL validator rejects URLs containing dots in path segments. Frappe's standard API method URL contains dots (`/api/method/some.module.function`), so you need to expose a clean path via your reverse proxy.

Add this to your nginx server block for the ERPNext site:

```nginx
location = /authnet_webhook {
    rewrite ^.*$ /api/method/erpnext_authorizenet.api.authnet_webhook break;
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

1. **Generate a Signature Key in Authorize.Net** (required before creating any webhook):
   - Account → Settings → API Credentials & Keys
   - Select "New Signature Key", check "Disable Old Signature Key", Submit
   - Copy the hex string — it's only shown once

2. **Create the Webhook in Authorize.Net**:
   - Account → Settings → Webhooks → Add Endpoint
   - Endpoint URL: `https://<your-domain>/authnet_webhook`
   - Status: Active
   - Subscribe to event: `net.authorize.payment.authcapture.created`
   - Save

3. **Configure ERPNext**:
   - Open **Authorize Net Settings**
   - Enter **API Login ID** and **Transaction Key** from your Authorize.Net merchant account
   - Enter the **Signature Key** from step 1
   - Set **Sandbox Mode** appropriately
   - Save — the Payment Gateway is registered automatically

4. **Create a Payment Gateway Account**:
   - List view → Payment Gateway Account → New
   - Payment Gateway: `Authorize.Net-<your gateway name>`
   - Payment Account: your bank or undeposited funds GL account
   - Save

## How It Works

1. ERPNext generates a Sales Invoice
2. A Payment Request is created, pointing to the Authorize.Net gateway
3. Customer clicks the payment link → ERPNext fetches a hosted payment token from Authorize.Net (refId = Integration Request name)
4. Customer is redirected to Authorize.Net's secure hosted form to enter card details
5. Authorize.Net processes payment and sends a signed (HMAC-SHA512) Webhook to `/authnet_webhook`
6. ERPNext verifies the signature, fetches transaction details to recover refId, matches to the original Integration Request, and creates a submitted Payment Entry

## apps.json entry

```json
{
  "url": "https://github.com/Mseethaler/erpnext_authorizenet",
  "branch": "main"
}
```

## License

MIT
