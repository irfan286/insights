# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class InsightsDashboardFilterPreset(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        dashboard: DF.Link
        filter_values: DF.JSON | None
        for_user: DF.Link | None
        preset_name: DF.Data
    # end: auto-generated types

    def validate(self):
        self.preset_name = (self.preset_name or "").strip()
        if not self.preset_name:
            frappe.throw(_("Preset name is required"))

        if self.is_new():
            # a preset can only ever be "mine" (private) or "everyone's" (shared) —
            # never assigned to a different user than whoever is creating it
            self.for_user = frappe.session.user if self.for_user else ""
        elif self.for_user and self.for_user != self.owner:
            frappe.throw(_("A preset cannot be marked private for a user other than its owner"))

        if not frappe.has_permission("Insights Dashboard v3", ptype="read", doc=self.dashboard):
            frappe.throw(
                _("You do not have permission to save filters for this dashboard"),
                frappe.PermissionError,
            )

    def on_trash(self):
        # a deleted preset must never be silently auto-applied again, so drop any
        # per-user "default preset" pointer(s) that reference this row
        from insights.insights.doctype.insights_dashboard_v3.insights_dashboard_v3 import (
            get_default_preset_key,
        )

        frappe.db.delete(
            "DefaultValue",
            {"defkey": get_default_preset_key(self.dashboard), "defvalue": self.name},
        )


def has_permission(doc, ptype, user):
    """Controller permission hook — can only DENY beyond whatever the role-based
    DocPerm already grants (frappe.permissions.has_controller_permissions)."""
    if user == "Administrator":
        return True
    if doc.owner == user or (doc.for_user and doc.for_user == user):
        return True
    if not doc.for_user:
        # shared preset: readable by anyone who can read the dashboard;
        # deletable by anyone who can edit the dashboard (curation of stray presets)
        if ptype == "read":
            return bool(
                frappe.has_permission("Insights Dashboard v3", ptype="read", doc=doc.dashboard, user=user)
            )
        if ptype == "delete":
            return bool(
                frappe.has_permission("Insights Dashboard v3", ptype="write", doc=doc.dashboard, user=user)
            )
    return False


def get_permission_query_conditions(user=None):
    from insights.permissions import InsightsPermissions

    user = user or frappe.session.user
    if user == "Administrator":
        return ""

    readable_dashboards = InsightsPermissions(user)._build_dashboard_permission_query("read").run(pluck="name")
    dashboards_sql = ", ".join(frappe.db.escape(d) for d in readable_dashboards) or "''"

    return f"""(
        `tabInsights Dashboard Filter Preset`.owner = {frappe.db.escape(user)}
        or `tabInsights Dashboard Filter Preset`.for_user = {frappe.db.escape(user)}
        or (
            ifnull(`tabInsights Dashboard Filter Preset`.for_user, '') = ''
            and `tabInsights Dashboard Filter Preset`.dashboard in ({dashboards_sql})
        )
    )"""
