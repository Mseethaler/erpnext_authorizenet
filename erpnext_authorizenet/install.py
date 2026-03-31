"""
install.py

Runs on `bench --site <site> install-app erpnext_authorizenet`.
Sets up any required initial configuration.
"""

import frappe
from frappe import _


def after_install():
	"""Called after the app is installed on a site."""
	frappe.db.commit()
	print("✓ ERPNext Authorize.Net Gateway installed.")
	print("  → Go to 'Authorize Net Settings' to enter your API credentials.")
	print("  → Go to 'NMI Settings' to configure NMI (when credentials are ready).")
	print("  → Each Settings record auto-registers a Payment Gateway on Save.")


def before_uninstall():
	"""Called before the app is removed from a site."""
	# Remove Payment Gateway records created by this app
	for gateway_prefix in ["Authorize.Net-", "NMI-"]:
		gateways = frappe.get_all(
			"Payment Gateway",
			filters={"gateway": ["like", f"{gateway_prefix}%"]},
			pluck="name",
		)
		for gw in gateways:
			frappe.delete_doc("Payment Gateway", gw, ignore_missing=True)

	frappe.db.commit()
	print("✓ ERPNext Authorize.Net Gateway uninstalled. Payment Gateway records removed.")
