app_name = "erpnext_authorizenet"
app_title = "ERPNext Authorize.Net Gateway"
app_publisher = "Digital Sovereignty"
app_description = "Authorize.Net and NMI payment gateway integration for ERPNext"
app_email = "service@digital-sovereignty.cc"
app_license = "MIT"
app_version = "0.1.0"

# Frappe version compatibility
required_apps = ["frappe", "erpnext"]

# DocTypes for this app
# ----------------------------------------------------------
# These are loaded automatically from the doctype directories.

# Website route rules
# ----------------------------------------------------------
website_route_rules = [
	{
		"from_route": "/authorizenet_checkout",
		"to_route": "authorizenet_checkout",
	},
	{
		"from_route": "/authorizenet_return",
		"to_route": "authorizenet_return",
	},
	{
		"from_route": "/nmi_checkout",
		"to_route": "nmi_checkout",
	},
]

# Whitelisted API methods (callable via /api/method/...)
# ----------------------------------------------------------
# Payment webhook — must be allow_guest since Authorize.Net posts to it
# Declared on the function itself via @frappe.whitelist(allow_guest=True)

# on_session_creation hooks, etc. are not needed for a gateway app.

# Scheduled tasks
# ----------------------------------------------------------
# None required — payment reconciliation is webhook-driven.

# Install / uninstall hooks
# ----------------------------------------------------------
after_install = "erpnext_authorizenet.install.after_install"
before_uninstall = "erpnext_authorizenet.install.before_uninstall"
