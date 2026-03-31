from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

setup(
	name="erpnext_authorizenet",
	version="0.1.0",
	description="Authorize.Net and NMI payment gateway integration for ERPNext",
	author="Digital Sovereignty",
	author_email="service@digital-sovereignty.cc",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires,
)
