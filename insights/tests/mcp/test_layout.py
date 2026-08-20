# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Dashboard grid placement.

The rule these tests exist to protect, from a bug report: adding one chart must not
rearrange the other five. `update_dashboard` used to reflow everything on every call,
so a dashboard a human had tidied came back scrambled.

Pure unit tests -- `layout` takes plain dicts and knows nothing about documents.
"""

from frappe.tests import UnitTestCase

from insights.mcp import layout

CHART_TYPES = {"number-chart": "Number"}


def chart(name="c1", **kwargs):
    return {"type": "chart", "chart": name, **kwargs}


def placed(item, x, y, w, h):
    return dict(item, layout={"i": "fixed", "x": x, "y": y, "w": w, "h": h})


class TestSizes(UnitTestCase):
    def test_a_chart_is_half_the_grid(self):
        self.assertEqual(layout.size_for(chart()), (10, 8))

    def test_a_number_chart_is_a_full_width_banner(self):
        self.assertEqual(
            layout.size_for(chart("number-chart"), chart_types=CHART_TYPES), (20, 3)
        )

    def test_width_full_widens_and_is_consumed(self):
        item = chart(width="full")
        self.assertEqual(layout.size_for(item), (20, 8))
        self.assertNotIn("width", item, "`width` must not reach the stored document")

    def test_text_and_filter_sizes(self):
        self.assertEqual(layout.size_for({"type": "text"}), (10, 1))
        self.assertEqual(layout.size_for({"type": "filter"}), (4, 1))


class TestPlaceNew(UnitTestCase):
    def test_an_existing_layout_is_left_exactly_as_it_was(self):
        arranged = placed(chart("a"), x=7, y=3, w=13, h=5)
        layout.place_new([arranged])
        self.assertEqual(arranged["layout"], {"i": "fixed", "x": 7, "y": 3, "w": 13, "h": 5})

    def test_a_new_chart_lands_below_everything_else(self):
        existing = placed(chart("a"), x=0, y=2, w=10, h=8)
        new = chart("b")
        layout.place_new([existing, new])
        self.assertEqual(existing["layout"]["y"], 2)
        self.assertEqual((new["layout"]["x"], new["layout"]["y"]), (0, 10))

    def test_two_new_charts_stack_rather_than_overlap(self):
        first, second = chart("a"), chart("b")
        layout.place_new([first, second])
        self.assertEqual(first["layout"]["y"], 0)
        self.assertEqual(second["layout"]["y"], 8)

    def test_a_new_item_gets_a_unique_id(self):
        items = [chart("a"), chart("b")]
        layout.place_new(items)
        ids = [i["layout"]["i"] for i in items]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(ids))

    def test_a_new_filter_joins_the_existing_filter_row(self):
        existing = placed({"type": "filter", "filter_name": "A"}, x=0, y=0, w=4, h=1)
        new = {"type": "filter", "filter_name": "B"}
        layout.place_new([existing, new])
        self.assertEqual((new["layout"]["x"], new["layout"]["y"]), (4, 0))
        self.assertEqual(existing["layout"]["x"], 0)

    def test_a_filter_row_that_is_full_pushes_the_board_down(self):
        """The one case where incremental placement moves an existing item -- and it is
        what the UI does (`positionNewFilter`, dashboard.ts:167-180). A filter placed
        off-grid would simply not be visible."""
        filters = [
            placed({"type": "filter", "filter_name": str(i)}, x=i * 4, y=0, w=4, h=1)
            for i in range(5)
        ]
        board = placed(chart("a"), x=0, y=1, w=10, h=8)
        new = {"type": "filter", "filter_name": "overflow"}

        layout.place_new([*filters, board, new])

        self.assertEqual((new["layout"]["x"], new["layout"]["y"]), (0, 0))
        self.assertTrue(all(f["layout"]["y"] == 1 for f in filters))
        self.assertEqual(board["layout"]["y"], 2)

    def test_the_first_filter_pushes_the_board_down_instead_of_overlapping_it(self):
        """A divergence from the UI, and a deliberate one: it leaves the new filter on
        top of the charts and lets the grid renderer sort it out. We have no renderer."""
        board = placed(chart("a"), x=0, y=0, w=10, h=8)
        new = {"type": "filter", "filter_name": "F"}
        layout.place_new([board, new])
        self.assertEqual((new["layout"]["x"], new["layout"]["y"]), (0, 0))
        self.assertEqual(board["layout"]["y"], 1)

    def test_a_board_well_below_the_filters_is_not_pushed(self):
        filters = [
            placed({"type": "filter", "filter_name": str(i)}, x=i * 4, y=0, w=4, h=1)
            for i in range(5)
        ]
        board = placed(chart("a"), x=0, y=6, w=10, h=8)
        layout.place_new([*filters, board, {"type": "filter", "filter_name": "overflow"}])
        self.assertEqual(board["layout"]["y"], 6)


class TestReflow(UnitTestCase):
    def test_it_lays_everything_out_left_to_right(self):
        items = [chart("a"), chart("b")]
        layout.reflow(items)
        self.assertEqual((items[0]["layout"]["x"], items[0]["layout"]["y"]), (0, 0))
        self.assertEqual((items[1]["layout"]["x"], items[1]["layout"]["y"]), (10, 0))

    def test_it_wraps_at_the_grid_width(self):
        items = [chart(str(i)) for i in range(3)]
        layout.reflow(items)
        self.assertEqual((items[2]["layout"]["x"], items[2]["layout"]["y"]), (0, 8))

    def test_filters_take_the_top_row(self):
        items = [chart("a"), {"type": "filter", "filter_name": "F"}]
        layout.reflow(items)
        self.assertEqual(items[1]["layout"]["y"], 0)
        self.assertEqual(items[0]["layout"]["y"], 1)

    def test_it_overwrites_an_existing_arrangement(self):
        """Stated as a test because it is the destructive behaviour update_dashboard
        deliberately no longer defaults to."""
        arranged = placed(chart("a"), x=7, y=3, w=13, h=5)
        layout.reflow([arranged])
        self.assertEqual((arranged["layout"]["x"], arranged["layout"]["y"]), (0, 0))

    def test_the_grid_item_id_survives_a_reflow(self):
        arranged = placed(chart("a"), x=7, y=3, w=13, h=5)
        layout.reflow([arranged])
        self.assertEqual(arranged["layout"]["i"], "fixed")
