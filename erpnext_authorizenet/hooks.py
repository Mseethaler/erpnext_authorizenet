app_name = "erpnext_authorizenet"
app_title = "ERPNext Authorize.Net Gateway"
app_publisher = "Digital Sovereignty"
app_description = "Authorize.Net and NMI payment gateway integration for ERPNext"
app_email = "service@digital-sovereignty.cc"
app_license = "MIT"
app_version = "0.1.0"

required_apps = ["frappe", "erpnext"]

# Website route rules — for the customer-facing checkout pages.
# Note: /authnet_webhook and /authnet_return are NOT routed here; they are
# nginx rewrites to API methods (see README), because Authorize.Net rejects
# URLs containing dots in path segments.
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

# Install / uninstall hooks
after_install = "erpnext_authorizenet.install.after_install"
before_uninstall = "erpnext_authorizenet.install.before_uninstall"
