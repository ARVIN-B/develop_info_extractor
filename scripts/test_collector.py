import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_sources import (
    SOURCES,
    argument_values,
    counts,
    is_mcp_error,
    references_for,
    requires_reference,
    serialize,
    tool_policy,
)


class ModelValue:
    def model_dump(self, **kwargs):
        return {"nested": [{"value": None}, {"value": "original"}], "metadata": {"x": 1}}


class CollectorTests(unittest.TestCase):
    def test_all_ten_source_output_names_are_configured(self):
        self.assertEqual(len(SOURCES), 10)
        self.assertEqual(len({item[2] for item in SOURCES}), 10)

    def test_serialization_keeps_nested_values(self):
        original = ModelValue().model_dump()
        self.assertEqual(serialize(ModelValue()), original)

    def test_identifier_is_not_invented(self):
        definition = {
            "inputSchema": {
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            }
        }
        arguments, missing = argument_values(definition)
        self.assertIsNone(arguments)
        self.assertEqual(missing, ["product_id"])

    def test_real_reference_can_fill_required_identifier(self):
        definition = {
            "inputSchema": {
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            }
        }
        arguments, missing = argument_values(definition, references=[123])
        self.assertEqual(arguments, {"product_id": 123})
        self.assertEqual(missing, [])

    def test_nullable_optional_query_uses_a_supported_test_value(self):
        definition = {
            "inputSchema": {
                "properties": {"query": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}},
                "required": [],
            }
        }
        arguments, missing = argument_values(definition)
        self.assertEqual(arguments, {"query": "کافه"})
        self.assertEqual(missing, [])

    def test_documented_array_reference_routes_real_ids(self):
        definition = {
            "name": "em_compare",
            "inputSchema": {
                "properties": {
                    "product_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
                },
                "required": ["product_ids"],
            },
        }
        refs = references_for(definition, [("em_search", {"products": [{"id": 10}, {"id": 11}]})])
        arguments, missing = argument_values(definition, references=refs)
        self.assertEqual(arguments, {"product_ids": [10, 11]})
        self.assertEqual(missing, [])

    def test_reference_array_respects_schema_maximum(self):
        definition = {"inputSchema": {"properties": {"products": {
            "type": "array", "items": {"description": "Product id or URL", "type": "string"}, "maxItems": 2,
        }}, "required": ["products"]}}
        arguments, missing = argument_values(definition, references=["a", "b", "c"])
        self.assertEqual(arguments, {"products": ["a", "b"]})
        self.assertEqual(missing, [])

    def test_reference_description_maps_parameter_alias_and_source_tool(self):
        definition = {"name": "bs_vendor", "inputSchema": {"properties": {
            "vendor": {"type": "string", "description": "shop_id from results, e.g. a shop."},
        }, "required": ["vendor"]}}
        refs = references_for(definition, [
            ("bs_search", {"products": [{"shop_id": "real-shop"}]}),
            ("tb_search", {"products": [{"shop_id": "wrong-source"}]}),
        ])
        self.assertEqual(refs, ["real-shop"])

    def test_schema_pattern_rejects_incompatible_real_reference(self):
        definition = {"name": "job_irantalent_company", "inputSchema": {"properties": {
            "company_id": {"type": "string", "pattern": "^[0-9a-fA-F-]{36}$", "description": "Company UUID from job_companies."},
        }, "required": ["company_id"]}}
        refs = references_for(definition, [("job_companies", {"companies": [{"id": 183458}]})])
        self.assertEqual(refs, [])

    def test_documented_example_can_fill_non_identifier_argument(self):
        definition = {
            "inputSchema": {
                "properties": {"origin": {"type": "string", "description": "Start 'lat,lng', e.g. '35.6997,51.3380'."}},
                "required": ["origin"],
            }
        }
        arguments, missing = argument_values(definition)
        self.assertEqual(arguments, {"origin": "35.6892,51.3890"})
        self.assertEqual(missing, [])

    def test_reference_is_taken_from_named_real_output(self):
        definition = {
            "name": "tb_product",
            "inputSchema": {
                "properties": {"product": {"type": "string", "description": "Product ID from tb_search."}},
                "required": ["product"],
            },
        }
        self.assertTrue(requires_reference(definition))
        self.assertEqual(references_for(definition, [("tb_search", {"results": [{"id": "real-id"}]})]), ["real-id"])

    def test_error_text_is_not_reported_as_success(self):
        self.assertTrue(is_mcp_error({
            "isError": False,
            "content": [{"text": "Error executing tool example: upstream error"}],
        }))

    def test_annotation_controls_safe_execution(self):
        self.assertEqual(tool_policy({"annotations": {"readOnlyHint": True}})[0], "execute")
        self.assertEqual(tool_policy({"annotations": {"destructiveHint": True}})[0], "skip")

    def test_counts_are_execution_only(self):
        record = {"tools": [{
            "execution_status": "attempted",
            "executions": [
                {"status": "success", "original_mcp_response": {"content": []}},
                {"status": "mcp_error", "original_mcp_response": {"content": []}},
                {"status": "failed", "original_mcp_response": None},
            ],
        }, {"execution_status": "skipped", "executions": []}]}
        self.assertEqual(counts(record), {
            "attempted": 3, "successful": 1, "failed": 2,
            "skipped": 1, "saved_responses": 2,
        })


if __name__ == "__main__":
    unittest.main()
