import frappe
from frappe import _
from frappe.utils import flt


def calculate_tax_before_discount(doc, method):
    """
    Hook: Sales Invoice - validate
    Recalculates taxes based on pre-discount item totals
    instead of the post-discount net total.
    """
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

    _recalculate_taxes(doc, pre_discount_total)
    _recalculate_totals(doc)

    frappe.msgprint(
        _("Taxes calculated on pre-discount amount: {0}").format(
            frappe.format_value(pre_discount_total, {"fieldtype": "Currency"})
        ),
        indicator="green",
        alert=True
    )


def _has_discount(doc):
    """
    Returns True if any discount exists at invoice or item level.
    Covers:
      - doc.additional_discount_percentage  (invoice-level %)
      - doc.discount_amount                 (invoice-level flat)
      - item.discount_percentage            (per-item %)
      - item.discount_amount                (per-item flat)
    """
    if flt(doc.discount_amount) or flt(doc.additional_discount_percentage):
        return True

    for item in doc.items:
        if flt(item.discount_percentage) or flt(item.discount_amount):
            return True

    return False


def _get_pre_discount_net_total(doc):
    """
    Computes the total BEFORE any discount is applied.

    Uses price_list_rate * qty per item — price_list_rate is the
    catalogue rate before item-level discount_percentage or
    discount_amount is deducted.

    Falls back to item.rate * qty if price_list_rate is not set
    (e.g. manually entered invoices with no price list).
    """
    total = 0.0
    for item in doc.items:
        base_rate = flt(item.price_list_rate) if flt(item.price_list_rate) else flt(item.rate)
        total += base_rate * flt(item.qty)
    return total


def _recalculate_taxes(doc, pre_discount_total):
    """
    Iterates over doc.taxes and recalculates each row using
    pre_discount_total as the base instead of net_total.

    charge_type handling:
      - 'On Net Total'  → recalculate using pre_discount_total
      - 'Actual'        → fixed amount, do not touch tax_amount;
                          only update running total fields
      - anything else   → skip (On Previous Row Total/Amount are
                          rare on Sales Invoices and cascade from
                          the first row; touching them separately
                          would double-correct)

    All monetary fields on the tax row are kept in sync:
      tax_amount, base_tax_amount,
      tax_amount_after_discount_amount,
      base_tax_amount_after_discount_amount,
      total, base_total
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
            # Fixed rupee/amount taxes — do not alter tax_amount.
            # Still update the running cumulative total field.
            running_total = flt(running_total + flt(tax.tax_amount), tax.precision("total"))
            tax.total      = running_total
            tax.base_total = running_total

        # On Previous Row Total / On Previous Row Amount — leave untouched.
        # They are edge cases; if needed, extend this block.


def _recalculate_totals(doc):
    """
    Recomputes invoice-level total fields after tax rows are adjusted.

    What we deliberately do NOT change:
      - doc.net_total      → discount already applied by ERPNext; correct
      - doc.total          → pre-tax item total; correct
      - item-level amounts → not our concern; only taxes change

    What we recalculate:
      - total_taxes_and_charges
      - grand_total / base_grand_total
      - rounding_adjustment
      - rounded_total / base_rounded_total
      - outstanding_amount
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

    # outstanding_amount follows rounded_total when rounding is active,
    # otherwise follows grand_total
    if not doc.disable_rounded_total:
        doc.outstanding_amount = rounded
    else:
        doc.outstanding_amount = grand_total