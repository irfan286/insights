"""The endpoints check the types their signatures already declare.

`insights_whitelist` used to wrap the endpoint in a `*args, **kwargs` function
and hand that to `frappe.whitelist`. Frappe reads the annotations off the
function it decorates, so it read an empty `__annotations__` and checked
nothing. It now decorates the endpoint itself.
"""

import unittest
from pathlib import Path

import frappe

from insights.api.shared import is_public
from insights.api.workbooks import (
    create_folder,
    delete_folder,
    import_workbook,
    move_item_to_folder,
    rename_folder,
    toggle_folder_expanded,
)
from insights.decorators import insights_whitelist
from insights.tests.base import InsightsIntegrationTestCase
from insights.tests.factories import (
    DT,
    USER_1,
    as_user,
    create_test_user,
    create_test_workbook,
    delete_users,
)


def frappe_refuses_unannotated_endpoints() -> bool:
    """Whether the installed frappe acts on `require_type_annotated_api_methods`.

    `hooks.py` declares the hook, but only the framework can enforce it, and a frappe
    that never reads it leaves an endpoint without annotations callable. This fork runs
    frappe v16.29.0, where the name appears nowhere: `validate_argument_types` returns
    early for a function whose `__annotations__` are empty, and nothing refuses it
    sooner. The endpoints that *do* declare types are still checked -- the other tests
    here pin that -- so what is missing is the guard against a future endpoint that
    forgets to declare any.

    Read from source rather than pinned to a version, so the test starts running again
    on a frappe that implements it.
    """
    root = Path(frappe.__file__).parent
    candidates = (root / "handler.py", root / "__init__.py", root / "utils" / "typing_validations.py")
    return any(
        "require_type_annotated_api_methods" in path.read_text(errors="ignore")
        for path in candidates
        if path.exists()
    )

OWNER = USER_1


class EndpointsCheckArgumentTypes(InsightsIntegrationTestCase):
    @classmethod
    def before_class(cls):
        create_test_user(OWNER)
        cls.workbook = create_test_workbook(OWNER).name
        with as_user(OWNER):
            cls.folder = create_folder(cls.workbook, "Arg Types Folder", "query")

    @classmethod
    def after_class(cls):
        frappe.delete_doc(DT.WORKBOOK, cls.workbook, force=True, delete_permanently=True)
        delete_users(OWNER)

    def test_frappe_checks_an_insights_whitelisted_signature(self):
        @insights_whitelist()
        def takes_a_name(name: str):
            return name

        with as_user(OWNER):
            self.assertEqual(takes_a_name(name="a name"), "a name")
            with self.assertRaises(TypeError):
                takes_a_name(name={"title": "not a name"})

    def test_a_positional_argument_is_checked_too(self):
        """Frappe names positional arguments through `__code__`, which a `*args`
        wrapper does not carry. Passing one hides the check if it ever returns."""

        @insights_whitelist()
        def takes_a_name(name: str):
            return name

        with as_user(OWNER), self.assertRaises(TypeError):
            takes_a_name({"title": "not a name"})

    @unittest.skipUnless(
        frappe_refuses_unannotated_endpoints(),
        "this frappe does not implement require_type_annotated_api_methods",
    )
    def test_an_unannotated_endpoint_is_refused(self):
        """`require_type_annotated_api_methods` is on, so frappe refuses one."""

        @insights_whitelist()
        def takes_anything(name):
            return name

        with as_user(OWNER), self.assertRaises(TypeError):
            takes_anything(name="a name")

    def test_a_filter_set_is_not_a_folder_name(self):
        """A dict reaches `frappe.get_doc` as a filter set, so it is not a name."""
        with as_user(OWNER), self.assertRaises(TypeError):
            move_item_to_folder("query", {"title": "Arg Types Folder"})

        with as_user(OWNER), self.assertRaises(TypeError):
            rename_folder({"title": "Arg Types Folder"}, "renamed")

    def test_a_workbook_file_arrives_as_json_text_or_as_a_dict(self):
        """`import_workbook` starts with `frappe.parse_json`, so both are names
        for the same file. The annotation used to admit only a dict."""
        with as_user(OWNER), self.assertRaises(KeyError):
            import_workbook("{}")

    def test_a_flag_arrives_as_true_or_as_1(self):
        """JSON carries a flag either way, and `isinstance(1, bool)` is False."""
        with as_user(OWNER):
            toggle_folder_expanded(self.folder, 1)
            self.assertEqual(frappe.db.get_value("Insights Folder", self.folder, "is_expanded"), 1)
            toggle_folder_expanded(self.folder, False)
            self.assertEqual(frappe.db.get_value("Insights Folder", self.folder, "is_expanded"), 0)

    def test_a_delete_flag_arrives_as_1(self):
        with as_user(OWNER):
            folder = create_folder(self.workbook, "Arg Types Doomed Folder", "query")
            delete_folder(folder, 1)
            self.assertFalse(frappe.db.exists("Insights Folder", folder))

    def test_a_name_from_a_json_blob_is_still_a_name(self):
        """`run_doc_method` reads `name` out of a payload frappe checks only as
        a whole, so a dict can reach `frappe.db.exists` as a filter set."""
        with as_user(OWNER):
            self.assertFalse(is_public("Insights Chart v3", {"is_public": 1}))
