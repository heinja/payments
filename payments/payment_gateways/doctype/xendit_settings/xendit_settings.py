# Copyright (c) 2023, Frappe Technologies and contributors
# For license information, please see license.txt

import json
from urllib.parse import urlencode

import frappe
import xendit
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway

global payment_gateway_doctype, payment_gateway_document
payment_gateway_doctype = "Xendit Settings"
payment_gateway_document = "Xendit"

api_path = (
    "/api/method/payments.payment_gateways.doctype.xendit_settings.xendit_settings"
)


class XenditSettings(Document):
    supported_currencies = ["IDR"]

    # A required method, perform the following global-search to track where the method is being used:
    # Keyword: "controller.validate_transaction_currency("
    # Exclude: "*_settings, *archived"
    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. Xendit does not support transactions in currency '{0}'"
                ).format(currency)
            )

    # A required method, perform the following global-search to track where the method is being used:
    # Keyword: "controller.get_payment_url("
    # Exclude: "*_settings, *archived"
    def get_payment_url(self, **kwargs):
        # Decoding all ASCII values of kwargs
        kwargs["title"] = kwargs["title"].decode("ASCII")
        kwargs["description"] = kwargs["description"].decode("ASCII")
        kwargs["payer_name"] = kwargs["payer_name"].decode("ASCII")

        response = self.execute_set_express_checkout(**kwargs)

        kwargs.update({"is_remote_request": 1})

        create_request_log(
            kwargs,
            service_name="Xendit",
            name=response["external_id"],
            output=response,
        )

        return response["invoice_url"]

    def execute_set_express_checkout(self, **kwargs):
        # Setup Xendit instance
        api_key = self.get_password(fieldname="api_secret", raise_exception=False)
        xendit_instance = xendit.Xendit(api_key)

        # Fetching linked Payment Request document object
        try:
            payment_request = frappe.get_doc(
                "Payment Request", kwargs["reference_docname"]
            )
        except:
            frappe.throw("Referenced doctype for checkout is not a Payment Request")

        # Fetching linked Sales Order document object
        try:
            sales_order = frappe.get_doc("Sales Order", payment_request.reference_name)
        except:
            frappe.throw("Doctype referenced by Payment Request is not a Sales Order")

        # Building items parameter for Xendit invoice
        items = []
        for item in sales_order.items:
            items.append(
                {
                    "name": frappe.get_doc("Item", item.item_code).name,
                    "price": item.rate,
                    "quantity": item.qty,
                }
            )

        # Building customer parameter for Xendit invoice
        customer = {
            "given_names": kwargs["payer_name"],
            "email": kwargs["payer_email"],
        }
        mobile_number = frappe.get_doc("Customer", sales_order.customer).mobile_no
        if mobile_number != None:
            customer["mobile_number"] = mobile_number

        # Charging payment gateway fee to the paying customer
        fees = []
        fee = {
            "type": "GATEWAY",
            "value": 2000
            + int((((2.9 / 100) * payment_request.grand_total) / 1000)) * 1000,
        }
        fees.append(fee)

        # Arranging success & failure redirect URLs
        payment_confirmation_url = get_url(
            f"{api_path}.confirm_payment?token={payment_request.name}"
        )

        try:
            invoice = xendit_instance.Invoice.create(
                external_id=payment_request.name,
                payer_email=kwargs["payer_email"],
                description=kwargs["description"],
                amount=kwargs["amount"],
                customer=customer,
                should_send_email=True,
                invoice_duration=600,
                success_redirect_url=payment_confirmation_url,
                failure_redirect_url=payment_confirmation_url,
                currency="IDR",
                fees=fees,
                items=items,
            )
        except:
            frappe.throw("Failed to create Xendit Invoice, please check your settings")

        invoice = json.loads(f"""{invoice}""")

        return invoice

    def get_redirect_uri(doc, integration_request_id):
        return get_url(f"{api_path}.confirm_payment?token={integration_request_id}")

    def get_xendit_params_and_url(self):
        params = {"USER": self.api_secret}

        api_url = "https://api-3t.xendit.com/nvp"

        return params, api_url

    def validate(self):
        # Creating/updating Payment Gateway document with details according to the settings
        # See payments > utils > utils.py > create_payment_gateway() for more information
        create_payment_gateway(payment_gateway_document)

        # Calling erpnext.accounts.utils.create_payment_gateway_account to create/update Payment Gateway Account document according to the settings
        # "payment_gateway_enabled" hook definition can be seen at erpnext > hooks.py
        # Currently calls erpnext > accounts > utils.py > create_payment_gateway_account()
        call_hook_method("payment_gateway_enabled", gateway=payment_gateway_document)

        self.validate_xendit_credentials()

    def validate_xendit_credentials(self):
        xendit_instance = xendit.Xendit(api_key=self.api_secret)

        try:
            xendit_instance.Invoice.list_all()
        except:
            frappe.throw("Something went wrong in validating Xendit credentials")


def update_integration_request_status(token, data, status, error=False, doc=None):
    if not doc:
        doc = frappe.get_doc("Integration Request", token)

    doc.update_status(data, status)


@frappe.whitelist(allow_guest=True, xss_safe=True)
def confirm_payment(token):
    # Setup Xendit instance
    api_key = frappe.get_doc("Xendit Settings").get_password(
        fieldname="api_secret", raise_exception=False
    )
    xendit_instance = xendit.Xendit(api_key)

    try:
        integration_request = frappe.get_doc("Integration Request", token)
        invoice_id = json.loads(integration_request.output)["id"]
        invoice = json.loads(
            f"""{xendit_instance.Invoice.get(invoice_id=invoice_id)}"""
        )
    except:
        frappe.throw("Failed to fetch Xendit Invoice, please check your settings")

    try:
        if invoice["status"] == "PAID":
            update_integration_request_status(
                token,
                {},
                "Completed",
            )

            custom_redirect_to = frappe.get_doc(
                integration_request.get("reference_doctype"),
                integration_request.get("reference_docname"),
            ).run_method("on_payment_authorized", "Completed")
            frappe.db.commit()

            redirect_url = "payment-success?doctype={}&docname={}".format(
                integration_request.get("reference_doctype"),
                integration_request.get("reference_docname"),
            )
        else:
            redirect_url = "payment-failed"

        setup_redirect(integration_request, redirect_url, custom_redirect_to)

    except Exception:
        frappe.log_error(frappe.get_traceback())


def setup_redirect(data, redirect_url, custom_redirect_to=None, redirect=True):
    redirect_to = data.get("redirect_to") or None
    redirect_message = data.get("redirect_message") or None

    if custom_redirect_to:
        redirect_to = custom_redirect_to

    if redirect_to:
        redirect_url += "&" + urlencode({"redirect_to": redirect_to})
    if redirect_message:
        redirect_url += "&" + urlencode({"redirect_message": redirect_message})

    # this is done so that functions called via hooks can update flags.redirect_to
    if redirect:
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = get_url(redirect_url)
