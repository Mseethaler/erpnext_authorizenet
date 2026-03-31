# ERPNext Authorize.Net & NMI Payment Gateway

A Frappe/ERPNext app that adds Authorize.Net (and NMI) payment gateway support, for businesses that cannot use Stripe, PayPal, or Square — including firearms dealers, outdoor retailers, and other 2A-adjacent trades.

Built by [Digital Sovereignty](https://digital-sovereignty.cc) as a drop-in companion to the `payments` app.

## Gateways Included

- **Authorize.Net** (Accept Hosted — PCI-compliant hosted payment form)
- **NMI** (Network Merchants Inc.) — *stub ready, activate when credentials are available*

## Installation

```bash
# From your bench directory
bench get-app https://github.com/digital-sovereignty/erpnext_authorizenet
bench --site <yoursite> install-app erpnext_authorizenet
bench --site <yoursite> migrate
```

## Configuration

1. Go to **Authorize.Net Settings** in ERPNext
2. Enter your **API Login ID** and **Transaction Key** from your Authorize.Net merchant account
3. Set **Sandbox Mode** to "Yes" while testing, "No" for live transactions
4. Save — the Payment Gateway is registered automatically

## How It Works

The flow mirrors the Stripe integration exactly:

1. ERPNext generates a Sales Invoice
2. A Payment Request is created, pointing to the Authorize.Net gateway
3. Customer clicks the payment link → Authorize.Net generates a hosted payment token
4. Customer is redirected to Authorize.Net's secure hosted form to enter card details
5. Authorize.Net posts back to ERPNext's webhook endpoint
6. ERPNext creates a Payment Entry and marks the invoice as paid

## apps.json entry

```json
{
  "url": "https://github.com/Mseethaler/erpnext_authorizenet",
  "branch": "main"
}
```

## License

MIT
