import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analyzer import analyze  # noqa: E402
from app.expressions import parse, expression_text  # noqa: E402
from app.runtime import evaluate  # noqa: E402
from app.units import UNITS, convert, dim  # noqa: E402


def rules(*fields):
    return {"fields": list(fields)}


def raw(name, unit):
    return {"name": name, "kind": "raw", "unit": unit}


def derived(name, expression, output_unit=None):
    spec = {"name": name, "kind": "derived", "expression": expression}
    if output_unit:
        spec["output_unit"] = output_unit
    return spec


def codes(analyzer):
    return [e.code for e in analyzer.errors]


class UnitConversionTests(unittest.TestCase):
    def test_registered_units_are_canonically_consistent(self):
        """Each symbol must equal its stated scale in SI base units."""
        from fractions import Fraction
        from app.units import canonical_symbol

        cases = {
            "km": (1, 1000), "h": (1, 3600), "kWh": (1, 3_600_000),
            "km/h": (1, Fraction(5, 18)), "kWh/km": (1, 3600),
            "kWh/100km": (1, 36), "Wh/km": (1, 3.6),
            "L/km": (1, Fraction(1, 1_000_000)),
            "L/100km": (1, Fraction(1, 100_000_000)),
            "CNY/km": (1, Fraction(1, 1000)),
            "CNY/kWh": (1, Fraction(1, 3_600_000)),
            "CNY/L": (1, 1000), "CNY/h": (1, Fraction(1, 3600)),
            "CNY/t": (1, Fraction(1, 1000)),
        }
        for symbol, (value, expected) in cases.items():
            unit = UNITS[symbol]
            base = unit.canonical()
            converted, _ = convert(value, unit, base)
            got = Fraction(converted).limit_denominator(10 ** 9)
            self.assertEqual(unit.symbol, symbol)
            self.assertTrue(
                abs(float(got - Fraction(expected))) < 1e-9,
                f"{symbol}: {float(got)} != {float(expected)} "
                f"({canonical_symbol(unit.dims, unit.kind, unit.currency)})",
            )

    def test_price_units_cancel_to_money(self):
        value, _ = convert(0.65, UNITS["CNY/kWh"], UNITS["CNY/J"])
        self.assertAlmostEqual(value, 0.65 / 3_600_000)
        rs = rules(raw("fee", "CNY"), raw("distance", "km"),
                   derived("per_km", "fee / distance", "CNY/km"))
        ds = {"columns": ["fee", "distance"], "rows": [[68, 120]]}
        ev, _ = evaluate(rs, ds)
        self.assertAlmostEqual(ev.row_outputs["per_km"][0]["value"],
                               68 / 120)

    def test_kmh_to_ms(self):
        value, steps = convert(36, UNITS["km/h"], UNITS["m/s"])
        self.assertAlmostEqual(value, 10.0)
        self.assertTrue(steps)
        self.assertIn("km/h", steps[0]["formula"])

    def test_kmh_runtime_value(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("speed", "distance / duration", "km/h"),
                   derived("speed_ms", 'TO(speed, "m/s")', "m/s"))
        ds = {"columns": ["distance", "duration"], "rows": [[120, 2]]}
        ev, _ = evaluate(rs, ds)
        self.assertEqual(ev.errors, [])
        self.assertAlmostEqual(ev.row_outputs["speed"][0]["value"], 60.0)
        self.assertAlmostEqual(ev.row_outputs["speed_ms"][0]["value"],
                               60.0 / 3.6)

    def test_mph_conversion(self):
        value, _ = convert(60, UNITS["mph"], UNITS["km/h"])
        self.assertAlmostEqual(value, 96.56064)

    def test_volume_and_energy(self):
        value, _ = convert(1, UNITS["L"], UNITS["m³"])
        self.assertAlmostEqual(value, 0.001)
        value, _ = convert(1, UNITS["kWh"], UNITS["J"])
        self.assertAlmostEqual(value, 3_600_000)

    def test_temperature_absolute(self):
        value, steps = convert(20, UNITS["°C"], UNITS["K"])
        self.assertAlmostEqual(value, 293.15)
        value, steps = convert(20, UNITS["°C"], UNITS["°F"])
        self.assertAlmostEqual(value, 68.0)
        self.assertEqual(len(steps), 2)

    def test_temperature_delta_conversion(self):
        value, _ = convert(10, UNITS["Δ°C"], UNITS["Δ°F"])
        self.assertAlmostEqual(value, 18.0)

    def test_affine_mismatch_rejected(self):
        from app.errors import EngineError
        with self.assertRaises(EngineError) as ctx:
            convert(20, UNITS["°C"], UNITS["Δ°C"])
        self.assertEqual(ctx.exception.code, "TEMPERATURE_KIND_MISMATCH")


class TemperatureSemanticsTests(unittest.TestCase):
    def test_difference_of_absolute_temperatures_is_delta(self):
        rs = rules(raw("t_air", "°C"), raw("t_water", "°C"),
                   derived("delta", "t_air - t_water", "Δ°C"))
        ds = {"columns": ["t_air", "t_water"], "rows": [[32, 21.5]]}
        ev, analyzer = evaluate(rs, ds)
        self.assertEqual(codes(analyzer), [])
        self.assertAlmostEqual(ev.row_outputs["delta"][0]["value"], 10.5)
        self.assertEqual(analyzer.types["delta"].kind, "temperature_delta")

    def test_absolute_temperature_cannot_multiply(self):
        rs = rules(raw("t", "°C"), derived("doubled", "t * 2", "°C"))
        analyzer = analyze(rs)
        self.assertIn("AFFINE_TEMPERATURE_MULTIPLY", codes(analyzer))
        err = [e for e in analyzer.errors
               if e.code == "AFFINE_TEMPERATURE_MULTIPLY"][0]
        self.assertEqual(err.field, "doubled")
        self.assertEqual(err.expression, "t * 2")

    def test_absolute_temperatures_cannot_add(self):
        rs = rules(raw("a", "°C"), raw("b", "°C"),
                   derived("sum", "a + b", "°C"))
        analyzer = analyze(rs)
        self.assertIn("AFFINE_TEMPERATURE_ADD", codes(analyzer))

    def test_absolute_plus_delta_keeps_absolute(self):
        rs = rules(raw("t", "K"), raw("a", "°C"), raw("b", "°C"),
                   derived("delta", "a - b", "Δ°C"),
                   derived("shifted", "t + delta", "K"))
        analyzer = analyze(rs)
        self.assertEqual(codes(analyzer), [])
        self.assertEqual(analyzer.types["shifted"].kind, "temperature_abs")

    def test_kelvin_can_multiply(self):
        rs = rules(raw("t", "K"),
                   derived("scaled", 'TO(t, "K") * 2', "K"))
        analyzer = analyze(rs)
        self.assertEqual(codes(analyzer), [])

    def test_degree_celsius_output_unit_to_kelvin(self):
        rs = rules(raw("t", "°C"), derived("k", 'TO(t, "K")', "K"))
        ds = {"columns": ["t"], "rows": [[100]]}
        ev, _ = evaluate(rs, ds)
        self.assertAlmostEqual(ev.row_outputs["k"][0]["value"], 373.15)


class CurrencyAndTypeTests(unittest.TestCase):
    def test_cny_plus_usd_without_rate_rejected(self):
        rs = rules(raw("fee_cny", "CNY"), raw("fee_usd", "USD"),
                   derived("total", "fee_cny + fee_usd", "CNY"))
        analyzer = analyze(rs, {})
        self.assertIn("CURRENCY_RATE_MISSING", codes(analyzer))
        err = [e for e in analyzer.errors if e.code == "CURRENCY_RATE_MISSING"][0]
        self.assertEqual(err.field, "total")

    def test_cny_plus_usd_with_rate(self):
        rs = rules(raw("fee_cny", "CNY"), raw("fee_usd", "USD"),
                   derived("total", "fee_cny + fee_usd", "CNY"))
        ds = {"columns": ["fee_cny", "fee_usd"], "rows": [[100, 10]]}
        ev, analyzer = evaluate(rs, ds, {"USD>CNY": 7.2})
        self.assertEqual(codes(analyzer), [])
        self.assertAlmostEqual(ev.row_outputs["total"][0]["value"], 172.0)

    def test_money_cannot_multiply_money(self):
        rs = rules(raw("a", "CNY"), raw("b", "USD"),
                   derived("bad", "a * b", "CNY"))
        analyzer = analyze(rs, {"USD>CNY": 7.2})
        self.assertIn("MONEY_PRODUCT", codes(analyzer))

    def test_price_times_quantity_is_money(self):
        rs = rules(raw("usage", "kWh"), raw("tariff", "CNY/kWh"),
                   derived("cost", "usage * tariff", "CNY"))
        ds = {"columns": ["usage", "tariff"], "rows": [[100, 0.65]]}
        ev, analyzer = evaluate(rs, ds)
        self.assertEqual(codes(analyzer), [])
        self.assertAlmostEqual(ev.row_outputs["cost"][0]["value"], 65.0)
        self.assertEqual(ev.row_outputs["cost"][0]["unit"], "CNY")

    def test_ratio_arithmetic(self):
        rs = rules(raw("discount", "%"),
                   derived("payable", "1 - discount", "ratio"))
        ds = {"columns": ["discount"], "rows": [[90]]}
        ev, analyzer = evaluate(rs, ds)
        self.assertEqual(codes(analyzer), [])
        self.assertAlmostEqual(ev.row_outputs["payable"][0]["value"], 0.1)

    def test_count_is_not_dimensionless(self):
        rs = rules(raw("qty", "count"),
                   derived("seats", "qty + 2"))
        analyzer = analyze(rs)
        self.assertIn("UNIT_MISMATCH", codes(analyzer))

    def test_unknown_unit_not_treated_as_dimensionless(self):
        rs = rules({"name": "mystery", "kind": "raw", "unit": ""},
                   derived("out", "mystery * 2"))
        analyzer = analyze(rs)
        self.assertIn("FIELD_UNIT_UNKNOWN", codes(analyzer))


class ConditionalAndErrorTests(unittest.TestCase):
    def test_branch_incompatible_units_reported_with_downstream(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("branch", "IF distance > 0 [km] THEN 1 [km] ELSE 1 [h]"),
                   derived("downstream", "branch * 2"))
        analyzer = analyze(rs)
        self.assertIn("BRANCH_UNIT_MISMATCH", codes(analyzer))
        err = [e for e in analyzer.errors
               if e.code == "BRANCH_UNIT_MISMATCH"][0]
        self.assertEqual(err.field, "branch")
        self.assertEqual(err.expression,
                         "IF distance > 0 [km] THEN 1 [km] ELSE 1 [h]")
        self.assertEqual(err.affected, ["downstream"])
        self.assertEqual(analyzer.analysis["downstream"]["status"], "blocked")

    def test_division_by_zero_runtime(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("speed", "distance / duration", "km/h"))
        ds = {"columns": ["distance", "duration"], "rows": [[10, 2], [5, 0]]}
        ev, _ = evaluate(rs, ds)
        self.assertTrue(any(e.code == "DIVISION_BY_ZERO" for e in ev.errors))
        self.assertIn("row", ev.errors[0].details)
        self.assertIsNone(ev.row_outputs["speed"][1])

    def test_constant_division_by_zero_static(self):
        rs = rules(raw("distance", "km"),
                   derived("bad", "distance / 0"))
        analyzer = analyze(rs)
        self.assertIn("DIVISION_BY_ZERO", codes(analyzer))

    def test_cycle_blocks_publish(self):
        rs = rules(raw("distance", "km"),
                   derived("a", "b + 1 [km]", "km"),
                   derived("b", "a + 1 [km]", "km"))
        analyzer = analyze(rs)
        self.assertIn("DEPENDENCY_CYCLE", codes(analyzer))
        cycle = [e for e in analyzer.errors if e.code == "DEPENDENCY_CYCLE"][0]
        self.assertGreaterEqual(len(cycle.details["cycle"]), 3)

    def test_downstream_reanalysis_after_dependency_change(self):
        base = [raw("distance", "km"), raw("duration", "h"),
                derived("speed", "distance / duration", "km/h"),
                derived("speed_ms", 'TO(speed, "m/s")', "m/s"),
                derived("km_to_m", 'TO(distance, "m")', "m")]
        analyzer = analyze(rules(*base))
        self.assertEqual(codes(analyzer), [])
        broken = [dict(f) for f in base]
        for field in broken:
            if field["name"] == "duration":
                field["unit"] = "kg"
        analyzer = analyze(rules(*broken))
        err = [e for e in analyzer.errors if e.field == "speed"]
        self.assertTrue(err)
        # The dependency-graph change is re-checked through the whole chain.
        self.assertIn("speed_ms", err[0].affected)
        self.assertEqual(analyzer.analysis["speed_ms"]["status"], "blocked")
        # Unrelated siblings are still analysed and stay valid.
        self.assertEqual(analyzer.analysis["km_to_m"]["status"], "ok")

    def test_no_eval_of_arbitrary_code(self):
        rs = rules(raw("distance", "km"),
                   derived("bad", "__import__('os').system('echo hi')"))
        analyzer = analyze(rs)
        self.assertTrue(analyzer.errors)
        self.assertIn(analyzer.errors[0].code,
                      {"EXPR_SYNTAX", "UNKNOWN_REFERENCE"})

    def test_attribute_access_rejected(self):
        from app.errors import EngineError
        with self.assertRaises(EngineError):
            parse("distance.__class__")


class AggregateTests(unittest.TestCase):
    def test_sum_and_weighted_average(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("total_distance", "SUM(distance)", "km"),
                   derived("avg", "SUM(distance) / SUM(duration)", "km/h"))
        ds = {"columns": ["distance", "duration"],
              "rows": [[120, 2], [90, 1.5]]}
        ev, analyzer = evaluate(rs, ds)
        self.assertEqual(codes(analyzer), [])
        self.assertAlmostEqual(ev.scalar_outputs["total_distance"]["value"], 210)
        self.assertAlmostEqual(ev.scalar_outputs["avg"]["value"], 60)

    def test_mixing_row_and_aggregate_rejected(self):
        rs = rules(raw("distance", "km"),
                   derived("bad", "SUM(distance) + distance", "km"))
        analyzer = analyze(rs)
        self.assertIn("AGGREGATE_MIXED", codes(analyzer))

    def test_aggregate_declared_output_unit_converts(self):
        rs = rules(raw("energy", "kWh"),
                   derived("total", "SUM(energy)", "MWh"))
        ds = {"columns": ["energy"], "rows": [[1000], [500]]}
        ev, _ = evaluate(rs, ds)
        self.assertAlmostEqual(ev.scalar_outputs["total"]["value"], 1.5)

    def test_empty_sum_on_empty_dataset(self):
        rs = rules(raw("energy", "kWh"),
                   derived("total", "SUM(energy)", "kWh"))
        ev, _ = evaluate(rs, {"columns": ["energy"], "rows": []})
        self.assertTrue(any(e.code == "SUM_EMPTY_DATASET" for e in ev.errors))


class OutputUnitTests(unittest.TestCase):
    def test_output_unit_dimension_mismatch(self):
        rs = rules(raw("distance", "km"),
                   derived("bad", "distance", "kg"))
        analyzer = analyze(rs)
        self.assertIn("OUTPUT_UNIT_MISMATCH", codes(analyzer))

    def test_output_conversion_km_to_m(self):
        rs = rules(raw("distance", "km"),
                   derived("meters", "distance", "m"))
        ds = {"columns": ["distance"], "rows": [[1.5]]}
        ev, analyzer = evaluate(rs, ds)
        self.assertEqual(codes(analyzer), [])
        self.assertAlmostEqual(ev.row_outputs["meters"][0]["value"], 1500)

    def test_conversion_steps_present(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("speed", "distance / duration", "m/s"))
        ds = {"columns": ["distance", "duration"], "rows": [[36, 1]]}
        ev, _ = evaluate(rs, ds)
        steps = ev._row_steps["speed"][0]
        self.assertTrue(any("m/s" in s["formula"] for s in steps))
        self.assertAlmostEqual(ev.row_outputs["speed"][0]["value"], 10.0)


class StepTraceTests(unittest.TestCase):
    """Every printed step must be numerically true in its stated units."""

    def test_arithmetic_keeps_operand_units(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("speed", "distance / duration", "km/h"))
        ev, _ = evaluate(rs, {"columns": ["distance", "duration"],
                             "rows": [[120, 2]]})
        formulas = [s["formula"] for s in ev._row_steps["speed"][0]]
        self.assertEqual(formulas, ["120 km ÷ 2 h = 60 km/h"])

    def test_conversion_is_an_explicit_separate_step(self):
        rs = rules(raw("distance", "km"), raw("duration", "h"),
                   derived("speed", "distance / duration", "m/s"))
        ev, _ = evaluate(rs, {"columns": ["distance", "duration"],
                              "rows": [[120, 2]]})
        formulas = [s["formula"] for s in ev._row_steps["speed"][0]]
        self.assertEqual(len(formulas), 2)
        self.assertIn("120 km ÷ 2 h = 60 km/h", formulas[0])
        self.assertIn("16.66666667 m/s", formulas[1])

    def test_temperature_difference_labels_delta(self):
        rs = rules(raw("a", "°C"), raw("b", "°C"),
                   derived("d", "a - b", "Δ°C"))
        ev, _ = evaluate(rs, {"columns": ["a", "b"], "rows": [[32, 21.5]]})
        steps = ev._row_steps["d"][0]
        self.assertEqual(steps[-1]["formula"],
                         "32 °C - 21.5 °C = 10.5 Δ°C")
        self.assertEqual(steps[-1]["unit"], "Δ°C")

    def test_ratio_constant_adopts_percent_scale(self):
        rs = rules(raw("discount", "%"),
                   derived("payable", "1 - discount", "ratio"))
        ev, _ = evaluate(rs, {"columns": ["discount"], "rows": [[90]]})
        formulas = [s["formula"] for s in ev._row_steps["payable"][0]]
        self.assertIn("100 % - 90 % = 10 %", formulas)

    def test_currency_rate_step_is_shown(self):
        rs = rules(raw("cny", "CNY"), raw("usd", "USD"),
                   derived("total", "cny + usd", "CNY"))
        ev, _ = evaluate(rs, {"columns": ["cny", "usd"], "rows": [[68, 9.5]]},
                         {"USD>CNY": 7.2})
        formulas = [s["formula"] for s in ev._row_steps["total"][0]]
        self.assertIn("9.5 USD × 7.2 = 68.4 CNY", formulas)
        self.assertIn("68 CNY + 68.4 CNY = 136.4 CNY", formulas)


if __name__ == "__main__":
    unittest.main(verbosity=2)
