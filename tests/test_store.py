import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sample import (DEFAULT_FIELDS, DEFAULT_FX, DEFAULT_OUTPUT,
                        SAMPLE_COLUMNS, SAMPLE_NAME, SAMPLE_ROWS)
from app.store import Store


def temp_store():
    path = tempfile.mktemp(suffix=".db")
    return Store(path), path


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store, self.path = temp_store()
        self.dataset = self.store.create_dataset(
            SAMPLE_NAME, SAMPLE_COLUMNS, SAMPLE_ROWS)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_publish_is_versioned(self):
        first = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                   DEFAULT_FX, DEFAULT_OUTPUT)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["version"], 1)
        second = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                    DEFAULT_FX, DEFAULT_OUTPUT)
        self.assertEqual(second["version"], 2)
        self.assertIsNotNone(second["outputs"])
        self.assertEqual(second["outputs"], first["outputs"])

    def test_failed_publish_does_not_replace_latest(self):
        good = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                  DEFAULT_FX, DEFAULT_OUTPUT)
        bad_fields = [dict(f) for f in DEFAULT_FIELDS]
        for field in bad_fields:
            if field["name"] == "distance":
                field["unit"] = ""
        failed = self.store.publish(self.dataset["id"], bad_fields,
                                    DEFAULT_FX, DEFAULT_OUTPUT)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["version"], 2)
        latest = self.store.latest_version(self.dataset["id"])
        self.assertEqual(latest["version"], good["version"])
        self.assertIsNotNone(latest["outputs"])

    def test_preview_reports_rule_and_data_diff(self):
        base = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                  DEFAULT_FX, DEFAULT_OUTPUT)
        changed = [dict(f) for f in DEFAULT_FIELDS]
        for field in changed:
            if field["name"] == "speed_kmh":
                field["output_unit"] = "m/s"
        preview = self.store.preview(self.dataset["id"], changed,
                                     DEFAULT_FX, DEFAULT_OUTPUT)
        self.assertTrue(preview["valid"])
        names = [c["name"] for c in preview["diff"]["changed"]]
        self.assertIn("speed_kmh", names)
        data_changes = {c["field"]: c for c in
                        preview["data_diff"]["changes"]}
        self.assertIn("speed_kmh", data_changes)
        change = data_changes["speed_kmh"]
        self.assertEqual(change["unit_from"], "km/h")
        self.assertEqual(change["unit_to"], "m/s")
        self.assertEqual(change["affected_rows"], len(SAMPLE_ROWS))
        # preview must not persist anything
        self.assertEqual(self.store.list_versions(self.dataset["id"])[0]["version"],
                         base["version"])

    def test_preview_does_not_publish(self):
        self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                           DEFAULT_FX, DEFAULT_OUTPUT)
        before = len(self.store.list_versions(self.dataset["id"]))
        self.store.preview(self.dataset["id"], DEFAULT_FIELDS,
                           DEFAULT_FX, DEFAULT_OUTPUT)
        after = len(self.store.list_versions(self.dataset["id"]))
        self.assertEqual(before, after)

    def test_version_binds_rules_and_output(self):
        version = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                     DEFAULT_FX, DEFAULT_OUTPUT)
        loaded = self.store.get_version(self.dataset["id"], version["version"])
        self.assertEqual(loaded["fields"], DEFAULT_FIELDS)
        self.assertEqual(loaded["fx"], DEFAULT_FX)
        self.assertEqual(loaded["outputs"]["selected"], DEFAULT_OUTPUT)

    def test_currency_failure_blocks_publish(self):
        failed = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                    {}, DEFAULT_OUTPUT)
        self.assertEqual(failed["status"], "failed")
        codes = [i["code"] for i in failed["error"]["items"]]
        self.assertIn("CURRENCY_RATE_MISSING", codes)

    def test_cycle_blocks_publish(self):
        fields = [f for f in DEFAULT_FIELDS if f["kind"] == "raw"]
        fields += [
            {"name": "a", "kind": "derived", "expression": "b + 1 [km]",
             "output_unit": "km"},
            {"name": "b", "kind": "derived", "expression": "a + 1 [km]",
             "output_unit": "km"},
        ]
        failed = self.store.publish(self.dataset["id"], fields, DEFAULT_FX, "a")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("DEPENDENCY_CYCLE",
                      [i["code"] for i in failed["error"]["items"]])

    def test_sample_dataset_output_values(self):
        version = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                     DEFAULT_FX, DEFAULT_OUTPUT)
        rows = version["outputs"]["rows"]
        self.assertAlmostEqual(rows[0]["speed_kmh"], 60.0)
        self.assertAlmostEqual(rows[0]["speed_ms"], 60.0 / 3.6)
        self.assertAlmostEqual(rows[0]["temp_delta"], 10.5)
        self.assertAlmostEqual(rows[0]["cost_cny"], 15.6)
        # 68 CNY + 9.5 USD * 7.2
        self.assertAlmostEqual(rows[0]["total_fee_cny"], 136.4)
        self.assertAlmostEqual(rows[0]["energy_per_100km"], 20.0)
        aggregates = version["outputs"]["aggregates"]
        self.assertAlmostEqual(aggregates["total_distance"], 630.5)
        self.assertAlmostEqual(aggregates["total_energy"], 111.9)
        self.assertAlmostEqual(aggregates["avg_speed"], 630.5 / 10.45)

    def test_steps_include_conversion_chain(self):
        version = self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                                     DEFAULT_FX, DEFAULT_OUTPUT)
        steps = version["outputs"]["steps"]["speed_ms"]["rows"]
        formulas = [s.get("formula", "") for s in steps["0"]]
        self.assertTrue(any("m/s" in f for f in formulas))
        self.assertTrue(any("km" in f for f in formulas))

    def test_events_recorded(self):
        self.store.publish(self.dataset["id"], DEFAULT_FIELDS,
                           DEFAULT_FX, DEFAULT_OUTPUT)
        bad = [dict(f) for f in DEFAULT_FIELDS]
        for field in bad:
            if field["name"] == "duration":
                field["unit"] = ""
        self.store.publish(self.dataset["id"], bad, DEFAULT_FX, DEFAULT_OUTPUT)
        events = self.store.list_events(self.dataset["id"])
        kinds = [e["kind"] for e in events]
        self.assertIn("publish_success", kinds)
        self.assertIn("publish_failed", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
