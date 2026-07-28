import frappe

from insights.insights.doctype.insights_data_source_v3.ibis_utils import IbisQueryBuilder
from insights.tests.base import InsightsIntegrationTestCase


class TestIbisQueryBuilderGranularity(InsightsIntegrationTestCase):
    def make_query_doc(self, operations):
        return frappe._dict(
            name="Ibis Time Granularity Test",
            title="Ibis Time Granularity Test",
            use_live_connection=0,
            operations=frappe.as_json(operations),
        )

    def make_time_source_operations(self):
        return [
            {
                "type": "code",
                "code": """
results = [
    {"posting_time": "09:15:42.123", "label": "alpha"},
    {"posting_time": "09:15:42.987", "label": "beta"},
    {"posting_time": "14:33:19.111", "label": "gamma"},
]
""",
            },
            {
                "type": "cast",
                "column": {"type": "column", "column_name": "posting_time"},
                "data_type": "Time",
            },
        ]

    def build_query(self, operations):
        return IbisQueryBuilder(self.make_query_doc(operations)).build()

    def test_summary_query_groups_time_values_by_supported_granularities(self):
        cases = [
            ("hour", {"09:00:00": 2, "14:00:00": 1}),
            ("minute", {"09:15:00": 2, "14:33:00": 1}),
            ("second", {"09:15:42": 2, "14:33:19": 1}),
        ]

        for granularity, expected in cases:
            with self.subTest(granularity=granularity):
                query = self.build_query(
                    [
                        *self.make_time_source_operations(),
                        {
                            "type": "summarize",
                            "measures": [
                                {"measure_name": "row_count", "column_name": "label", "aggregation": "count"}
                            ],
                            "dimensions": [
                                {
                                    "column_name": "posting_time",
                                    "data_type": "Time",
                                    "granularity": granularity,
                                    "dimension_name": "posting_time_bucket",
                                }
                            ],
                        },
                        {
                            "type": "order_by",
                            "column": {"type": "column", "column_name": "posting_time_bucket"},
                            "direction": "asc",
                        },
                    ]
                )

                result = query.execute()
                actual = dict(zip(result["posting_time_bucket"], result["row_count"], strict=False))

                self.assertEqual(actual, expected)

    def test_summary_query_rejects_calendar_buckets_for_time_columns(self):
        operations = [
            *self.make_time_source_operations(),
            {
                "type": "summarize",
                "measures": [{"measure_name": "row_count", "column_name": "label", "aggregation": "count"}],
                "dimensions": [
                    {
                        "column_name": "posting_time",
                        "data_type": "Time",
                        "granularity": "month",
                        "dimension_name": "posting_time_bucket",
                    }
                ],
            },
        ]

        with self.assertRaises(frappe.ValidationError) as exc:
            self.build_query(operations)

        self.assertIn("Supported granularities: second, minute, hour", str(exc.exception))


class TestIbisQueryBuilderNestedFilters(InsightsIntegrationTestCase):
    def make_query_doc(self, operations):
        return frappe._dict(
            name="Ibis Nested Filter Test",
            title="Ibis Nested Filter Test",
            use_live_connection=0,
            operations=frappe.as_json(operations),
        )

    def build_query(self, operations):
        return IbisQueryBuilder(self.make_query_doc(operations)).build()

    def make_scd2_source_operations(self):
        # simulates an SCD2-style validity range table, where `valid_to` is
        # NULL for the row that is currently in effect
        return [
            {
                "type": "code",
                "code": """
results = [
    {"item": "expired", "valid_from": "2024-01-01", "valid_to": "2024-06-01"},
    {"item": "covers_as_of_date", "valid_from": "2024-01-01", "valid_to": "2024-12-01"},
    {"item": "current_open_ended", "valid_from": "2024-01-01", "valid_to": None},
    {"item": "not_yet_effective", "valid_from": "2025-01-01", "valid_to": None},
]
""",
            },
            {
                "type": "cast",
                "column": {"type": "column", "column_name": "valid_from"},
                "data_type": "Date",
            },
            {
                "type": "cast",
                "column": {"type": "column", "column_name": "valid_to"},
                "data_type": "Date",
            },
        ]

    def make_as_of_filter_group(self, as_of_date):
        return {
            "type": "filter_group",
            "logical_operator": "And",
            "filters": [
                {
                    "column": {"type": "column", "column_name": "valid_from"},
                    "operator": "<=",
                    "value": as_of_date,
                },
                {
                    "type": "filter_group",
                    "logical_operator": "Or",
                    "filters": [
                        {
                            "column": {"type": "column", "column_name": "valid_to"},
                            "operator": ">",
                            "value": as_of_date,
                        },
                        {
                            "column": {"type": "column", "column_name": "valid_to"},
                            "operator": "is_not_set",
                            "value": "",
                        },
                    ],
                },
            ],
        }

    def test_nested_filter_group_resolves_scd2_as_of_date(self):
        query = self.build_query(
            [
                *self.make_scd2_source_operations(),
                self.make_as_of_filter_group("2024-07-01"),
            ]
        )

        result = query.execute()
        self.assertEqual(
            set(result["item"]),
            {"covers_as_of_date", "current_open_ended"},
        )

    def test_nested_filter_group_supports_or_at_top_level(self):
        # as-of date before every row's valid_from, so the nested SCD2 group
        # matches nothing on its own — isolates this test to proving that a
        # top-level "Or" combining a plain rule with a nested group works
        query = self.build_query(
            [
                *self.make_scd2_source_operations(),
                {
                    "type": "filter_group",
                    "logical_operator": "Or",
                    "filters": [
                        {
                            "column": {"type": "column", "column_name": "item"},
                            "operator": "=",
                            "value": "expired",
                        },
                        self.make_as_of_filter_group("2023-01-01"),
                    ],
                },
            ]
        )

        result = query.execute()
        self.assertEqual(
            set(result["item"]),
            {"expired"},
        )

    def test_filter_group_nesting_beyond_max_depth_is_rejected(self):
        deepest = {
            "column": {"type": "column", "column_name": "item"},
            "operator": "=",
            "value": "expired",
        }
        nested = deepest
        # each wrap adds one level of nesting; go well past the limit so the
        # exact off-by-one at the boundary doesn't matter for this test
        for _ in range(IbisQueryBuilder.MAX_FILTER_GROUP_DEPTH + 5):
            nested = {"type": "filter_group", "logical_operator": "And", "filters": [nested]}

        with self.assertRaises(frappe.ValidationError) as exc:
            self.build_query([*self.make_scd2_source_operations(), nested])

        self.assertIn("cannot be nested more than", str(exc.exception))
