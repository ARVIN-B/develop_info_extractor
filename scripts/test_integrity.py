import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_sources import build_args, validate_run


class RunIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        base = self.root / "neshan-mcp"
        for folder in ("requests", "responses", "errors", "analysis"):
            (base / folder).mkdir(parents=True, exist_ok=True)
        (base / "status.json").write_text(json.dumps({
            "source": "neshan-mcp", "evidence_status": "verified_live_response"
        }), encoding="utf-8")
        (base / "requests" / "a.json").write_text(json.dumps({
            "source": "neshan-mcp", "timestamp": "2026-11-20T00:00:00Z",
            "tool": "ns_search", "arguments": {"query": "کافه"}
        }), encoding="utf-8")
        (base / "responses" / "a.json").write_text(json.dumps({
            "structuredContent": {"items": []}, "content": [], "isError": False
        }), encoding="utf-8")
        (self.root / "summary.json").write_text(json.dumps({"sources": [{
            "source": "neshan-mcp", "executed_request_count": 1,
            "actual_response_file_count": 1
        }]}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_run_passes_integrity_checks(self):
        self.assertEqual(validate_run(self.root), [])

    def test_response_without_request_is_rejected(self):
        (self.root / "neshan-mcp" / "requests" / "a.json").unlink()
        problems = validate_run(self.root)
        self.assertTrue(any("no corresponding request" in item for item in problems))

    def test_secret_like_value_is_rejected(self):
        path = self.root / "neshan-mcp" / "status.json"
        path.write_text(json.dumps({
            "source": "neshan-mcp", "evidence_status": "verified_live_response",
            "api_key": "not-a-real-key"
        }), encoding="utf-8")
        problems = validate_run(self.root)
        self.assertTrue(any("Potential secret value" in item for item in problems))

    def test_identifier_is_never_guessed(self):
        args, missing = build_args({
            "name": "dk_product",
            "inputSchema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]},
        })
        self.assertIsNone(args)
        self.assertEqual(missing, ["product_id"])


if __name__ == "__main__":
    unittest.main()
