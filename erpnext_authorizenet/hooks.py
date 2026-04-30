app_name = "erpnext_authorizenet"
app_title = "ERPNext Authorize.Net Gateway"
app_publisher = "Digital Sovereignty"
app_description = "Authorize.Net and NMI payment gateway integration for ERPNext"
app_email = "service@digital-sovereignty.cc"
app_license = "MIT"
app_version = "0.1.0"

# Frappe version compatibility
required_apps = ["frappe", "erpnext"]

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

# Request hooks
# ----------------------------------------------------------
# Authorize.Net's webhook URL validator rejects paths with dots, so we
# expose /authnet_webhook (clean path) and route it internally to the
# Frappe API method via this before_request hook. This means deployments
# don't need any nginx-level rewrite — the app itself handles the alias.
before_request = [
	"erpnext_authorizenet.api.handle_clean_path_webhook",
]

# Install / uninstall hooks
# ----------------------------------------------------------
after_install = "erpnext_authorizenet.install.after_install"
before_uninstall = "erpnext_authorizenet.install.before_uninstall"
