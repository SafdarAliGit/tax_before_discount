import frappe
import json
from frappe import _
from frappe.utils import flt
from erpnext.controllers.accounts_controller import get_taxes_and_charges
from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals


def calculate_tax_before_discount(doc, method):
    """
    Hook: Sales Order - validate
    1. Fetches discount_account from applied Pricing Rule per item.
    2. Recalculates taxes based on pre-discount item totals.
    """
    _set_discount_account_from_pricing_rule(doc)

    settings = frappe.get_single("Tax Before Discount Settings")

    if not settings.enabled:
        return

    if not settings.apply_to_all_companies:
        if doc.company != settings.company:
            return

    if not _has_discount(doc):
        return

    pre_discount_total = _get_pre_discount_net_total(doc)
    if not pre_discount_total:
        return

    post_discount_total = flt(doc.net_total)
    if not post_discount_total:
        return

    _set_order_booker(doc)
    _set_tax_template(doc)
    _recalculate_taxes(doc, pre_discount_total)
    _recalculate_totals(doc)

    frappe.msgprint(
        _("Taxes calculated on pre-discount amount: {0}").format(
            frappe.format_value(pre_discount_total, {"fieldtype": "Currency"})
        ),
        indicator="green",
        alert=True
    )


def _set_discount_account_from_pricing_rule(doc):
    """
    For each Sales Order item row:
      1. Try item.pricing_rules (JSON array or comma string)
      2. Try item.pricing_rule  (single value field)
      3. Fall back to DB lookup by item_code + company
    Sets item.discount_account if found and not already set.
    """
    for item in doc.items:
        # Do not overwrite if already set manually
        if item.get("discount_account"):
            continue

        discount_account = None

        # Strategy 1: item.pricing_rules field (ERPNext v15 stores as JSON array)
        rule_names = _parse_pricing_rules_field(item.get("pricing_rules"))
        if rule_names:
            discount_account = _fetch_discount_account_from_rules(rule_names)

        # Strategy 2: item.pricing_rule single field (fallback)
        if not discount_account:
            single_rule = item.get("pricing_rule")
            if single_rule:
                discount_account = _fetch_discount_account_from_rules([single_rule])

        # Strategy 3: lookup by item_code directly in Pricing Rule Item Code child table
        if not discount_account:
            discount_account = _fetch_discount_account_by_item(
                item.item_code,
                doc.company
            )

        if discount_account:
            item.discount_account = discount_account

        # DEBUG — remove after confirming it works
        frappe.log_error(
            title="SO Item Pricing Rule Debug",
            message=frappe.as_json({
                "item_code": item.item_code,
                "pricing_rules_raw": item.get("pricing_rules"),
                "pricing_rule_raw": item.get("pricing_rule"),
                "parsed_rules": _parse_pricing_rules_field(item.get("pricing_rules")),
                "discount_account_found": discount_account,
                "discount_account_on_item": item.get("discount_account")
            })
        )


def _parse_pricing_rules_field(value):
    """
    Parses the pricing_rules field which ERPNext v15 stores as:
      - None or empty string     → return []
      - JSON array string        → '["PRULE-0001"]' → ["PRULE-0001"]
      - Comma-separated string   → "PRULE-0001,PRULE-0002" → [...]
      - Plain single string      → "PRULE-0001" → ["PRULE-0001"]
    """
    if not value:
        return []

    value = value.strip()

    # Try JSON array first
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            return [r for r in parsed if r]
        except Exception:
            pass

    # Fall back to comma-separated or plain string
    return [r.strip() for r in value.split(",") if r.strip()]


def _fetch_discount_account_from_rules(rule_names):
    """
    Given a list of Pricing Rule names, returns the first
    discount_account found across those rules.
    Pricing Rule name is unique so no company filter needed.
    """
    for rule_name in rule_names:
        if not rule_name:
            continue
        result = frappe.db.get_value(
            "Pricing Rule",
            rule_name,
            "discount_account"
        )
        if result:
            return result
    return None


def _fetch_discount_account_by_item(item_code, company):
    """
    Strategy 3 fallback.
    Searches Pricing Rule Item Code child table for rules
    that apply to this item_code, then fetches discount_account
    from the parent Pricing Rule filtered by company, selling=1,
    not disabled, and discount_account is set.
    """
    if not item_code:
        return None

    rules_with_item = frappe.db.get_all(
        "Pricing Rule Item Code",
        filters={"item_code": item_code},
        pluck="parent"
    )

    if not rules_with_item:
        return None

    result = frappe.db.get_value(
        "Pricing Rule",
        {
            "name": ["in", rules_with_item],
            "company": company,
            "selling": 1,
            "disable": 0,
            "discount_account": ["not in", ["", None]]
        },
        "discount_account"
    )
    return result or None


def _has_discount(doc):
    """
    Returns True if any discount exists at order or item level.
    """
    if flt(doc.discount_amount) or flt(doc.additional_discount_percentage):
        return True

    for item in doc.items:
        if flt(item.discount_percentage) or flt(item.discount_amount):
            return True

    return False


def _get_pre_discount_net_total(doc):
    """
    Computes total BEFORE any discount.
    Uses price_list_rate * qty per item.
    Falls back to rate * qty if price_list_rate is not set.
    """
    total = 0.0
    for item in doc.items:
        base_rate = flt(item.price_list_rate) if flt(item.price_list_rate) else flt(item.rate)
        total += base_rate * flt(item.qty)
    return total


def _recalculate_taxes(doc, pre_discount_total):
    """
    Recalculates tax rows using pre_discount_total as base.

    charge_type handling:
      - On Net Total  → recalculate using pre_discount_total
      - Actual        → fixed amount, only update running total fields
      - anything else → skip
    """
    running_total = flt(pre_discount_total)

    for tax in doc.taxes:

        if tax.charge_type == "On Net Total":
            tax_rate = flt(tax.rate)
            new_tax_amount = flt(
                (tax_rate / 100) * pre_discount_total,
                tax.precision("tax_amount")
            )

            tax.tax_amount                            = new_tax_amount
            tax.base_tax_amount                       = new_tax_amount
            tax.tax_amount_after_discount_amount      = new_tax_amount
            tax.base_tax_amount_after_discount_amount = new_tax_amount

            running_total = flt(running_total + new_tax_amount, tax.precision("total"))
            tax.total      = running_total
            tax.base_total = running_total

        elif tax.charge_type == "Actual":
            running_total = flt(running_total + flt(tax.tax_amount), tax.precision("total"))
            tax.total      = running_total
            tax.base_total = running_total


def _recalculate_totals(doc):
    """
    Recomputes order-level totals after tax rows are adjusted.
    net_total (post-discount) is intentionally left intact.
    Sales Order has no outstanding_amount — that is invoice-only.
    """
    total_taxes = sum(flt(t.tax_amount) for t in doc.taxes)

    doc.total_taxes_and_charges      = flt(total_taxes, doc.precision("total_taxes_and_charges"))
    doc.base_total_taxes_and_charges = doc.total_taxes_and_charges

    grand_total = flt(flt(doc.net_total) + total_taxes, doc.precision("grand_total"))
    doc.grand_total      = grand_total
    doc.base_grand_total = flt(
        grand_total * flt(doc.conversion_rate or 1),
        doc.precision("base_grand_total")
    )

    rounded = flt(round(grand_total), doc.precision("rounded_total"))
    rounding_adj = flt(rounded - grand_total, doc.precision("rounding_adjustment"))

    doc.rounded_total            = rounded
    doc.base_rounded_total       = rounded
    doc.rounding_adjustment      = rounding_adj
    doc.base_rounding_adjustment = rounding_adj

def _set_order_booker(doc):
    if not doc.customer:
        return

    sales_team_rows = frappe.db.get_all(
        "Sales Team",
        filters={
            "parent": doc.customer,
            "parenttype": "Customer"
        },
        fields=["sales_person", "allocated_percentage"],
        order_by="idx asc"
    )

    if not sales_team_rows:
        return

    doc.set("sales_team", [])

    for row in sales_team_rows:
        if not row.sales_person:
            continue
        allocated_amount = (
            (doc.grand_total or 0) * (row.allocated_percentage or 0) / 100
        )
        doc.append("sales_team", {
            "sales_person":         row.sales_person,
            "allocated_percentage": row.allocated_percentage or 0,
            "allocated_amount":     allocated_amount,
        })

def _set_tax_template(doc):
    if not doc.customer:
        return

    tax_template = frappe.db.get_value(
        "Customer",
        doc.customer,
        "taxes_and_charges"
    )

    if tax_template:
        doc.taxes_and_charges = tax_template
        # Fetch and populate taxes child table rows from the template
        doc.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", tax_template))
        calculate_taxes_and_totals(doc)
    else:
        doc.taxes_and_charges = ""
        doc.set("taxes", [])
