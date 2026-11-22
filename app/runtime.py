"""Execution of a validated ruleset against a dataset.

Every value carries two things:

* ``canonical`` — the number in SI base units (kelvin for temperatures, a
  plain fraction for ratios).  Arithmetic is always done on this, so it is
  dimensionally correct;
* ``display`` — the concrete :class:`Unit` the value is currently expressed
  in while it flows through the expression.

Arithmetic combines the display units, so a step such as ``120 km ÷ 2 h``
reads ``= 60 km/h``: the number and the unit agree.  Canonical values are only
reintroduced at explicit conversion points (``TO(...)`` or the declared output
unit), and each conversion is recorded as its own auditable step.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import expressions as ex
from .analyzer import (QType, analyze, boolean_type as _boolean_type,
                       number_type as _number_type)
from .errors import EngineError
from .units import (DIM_NAMES, UNITS, Unit, canonical_symbol, convert, fmt,
                    temperature_family)

MAX_ROW_ERRORS = 8
OP_SYMBOLS = {"*": "×", "/": "÷"}

ZERO_DIMS = tuple(0 for _ in DIM_NAMES)
SCALAR_UNIT = Unit(ZERO_DIMS, 1, 0, "number", "1")


@dataclass
class Value:
    canonical: float
    qtype: QType
    display: Unit
    steps: list = None
    boolean: bool | None = None
    sample: list | None = None
    rows: int | None = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []

    def shown(self, unit=None):
        unit = unit or self.display
        return (self.canonical - float(unit.offset)) / float(unit.scale)


def _shown(canonical, unit):
    return (float(canonical) - float(unit.offset)) / float(unit.scale)


def _term(shown, unit):
    """Render an operand as ``number unit`` (bare numbers get no suffix)."""
    text = fmt(shown)
    symbol = getattr(unit, "symbol", "") or ""
    if symbol and symbol != "1":
        text += f" {symbol}"
    return text


def _dims_zero(dims):
    return all(x == 0 for x in dims)


# ---------------------------------------------------------------------------
# unit / type helpers
# ---------------------------------------------------------------------------


def _type_for_unit(u):
    return QType(u.dims, u.kind, currency=u.currency, unit_symbol=u.symbol)


def _copy_type(t):
    return QType(t.dims, t.kind, t.currency, t.unit_symbol, t.unknown,
                 t.boolean, t.detail)


def _delta_unit(unit):
    """The offset-free unit of the same scale, for temperature differences."""
    if unit.kind == "temperature_delta" or unit.symbol.startswith("Δ"):
        return unit
    symbol = "Δ" + unit.symbol
    found = UNITS.get(symbol)
    if found is not None:
        return found
    return Unit(unit.dims, unit.scale, 0, "temperature_delta", symbol)


def _registered(dims, scale, kind, currency=None):
    """Find a catalogue unit matching a composite's dimension *and* scale."""
    for u in UNITS.values():
        if u.dims != dims or (u.currency or None) != (currency or None):
            continue
        if u.kind != kind:
            continue
        if float(u.scale) == float(scale) and float(u.offset) == 0:
            return u
    return None


def _canonical_unit(dims, kind, currency=None):
    return Unit(dims, 1, 0, kind,
                canonical_symbol(dims, kind, currency), currency)


def composite_unit(left, right, op, qtype):
    """Display unit of ``left <op> right``.

    Returns ``(unit, exact)``.  ``exact`` is false when no registered unit has
    the combined scale, in which case the caller falls back to canonical
    numbers so the printed arithmetic stays truthful.
    """
    dims = qtype.dims
    want = (left.scale * right.scale if op == "*"
            else left.scale / right.scale)
    kind = qtype.kind
    found = _registered(dims, want, kind, qtype.currency)
    if found is not None:
        return found, True
    return _canonical_unit(dims, kind if kind != "money" else "money",
                           qtype.currency), False


def _product_type(left, right, op):
    dims = tuple(a + b if op == "*" else a - b
                 for a, b in zip(left.dims, right.dims))
    lk, rk = left.kind, right.kind
    if lk == "money" and rk == "money":
        kind, currency = "ratio", None
    elif lk == "money":
        kind, currency = "money", left.currency
    elif rk == "money":
        kind, currency = "money", right.currency
    elif op == "/" and _dims_zero(dims):
        if lk == rk == "count" or lk == rk == "ratio":
            kind, currency = "ratio", None
        elif lk == rk == "number":
            kind, currency = "number", None
        else:
            kind, currency = "ratio", None
    elif lk == "ratio" and rk in ("number", "ratio"):
        kind, currency = (right.kind if right.kind == "number" else "ratio"), None
    elif rk == "ratio" and lk in ("number", "ratio"):
        kind, currency = (left.kind if left.kind == "number" else "ratio"), None
    elif lk == "number":
        kind, currency = rk, right.currency
    elif rk == "number":
        kind, currency = lk, left.currency
    elif "count" in (lk, rk):
        kind, currency = "count", None
        if op == "/" and lk == rk == "count":
            kind = "ratio"
        elif op == "/" and rk == "count":
            kind = "quantity"
    else:
        kind, currency = "quantity", None
    symbol = None
    if kind == "money" and _dims_zero(dims):
        symbol = currency
    elif kind == "ratio":
        symbol = "ratio"
    return QType(dims, kind, currency=currency, unit_symbol=symbol)


# ---------------------------------------------------------------------------
# evaluator
# ---------------------------------------------------------------------------


class Evaluator:
    def __init__(self, ruleset, dataset, fx=None, analyzer=None):
        self.ruleset = ruleset
        self.dataset = dataset or {"columns": [], "rows": []}
        self.fx = fx or {}
        self.analyzer = analyzer or analyze(ruleset, self.fx)
        self.fields = {f["name"]: f for f in ruleset.get("fields", [])}
        self.rows = self.dataset.get("rows", [])
        self.columns = list(self.dataset.get("columns", []))
        self.col_index = {c: i for i, c in enumerate(self.columns)}
        self.row_outputs = {}
        self.row_values = {}
        self.scalar_outputs = {}
        self.values = {}
        self.field_reports = {}
        self.errors = []
        self._row_steps = {}
        self._error_counts = {}
        self.failed = set()

    # ------------------------------------------------------------------
    def run(self):
        for spec in self.ruleset.get("fields", []):
            name = spec.get("name")
            if not name or name in self.row_outputs or \
                    name in self.scalar_outputs or name in self.failed:
                continue
            self._eval_field(name)
        self._build_reports()
        return self

    # ------------------------------------------------------------------
    # errors
    # ------------------------------------------------------------------
    def _row_error(self, err, field, row=None):
        err.field = err.field or field
        key = (field, err.code)
        self._error_counts[key] = self._error_counts.get(key, 0) + 1
        if row is not None:
            err.details.setdefault("row", row + 1)
        err.affected = self.analyzer.downstream(field)
        self.errors.append(err)
        return err

    def _dependency_failed(self, ref, field, row=None):
        return EngineError(
            "DEPENDENCY_FAILED",
            f"依赖的字段 {ref} 计算失败，无法计算 {field}",
            field=field,
            details={"reference": ref,
                     "row": None if row is None else row + 1},
        )

    # ------------------------------------------------------------------
    # raw fields
    # ------------------------------------------------------------------
    def _raw_value(self, name, row):
        spec = self.fields[name]
        unit = UNITS.get(spec.get("unit"))
        if unit is None:
            raise EngineError(
                "FIELD_UNIT_UNKNOWN",
                f"字段 {name} 的原单位未登记或为空",
                field=name, details={"row": row + 1},
            )
        idx = self.col_index.get(name)
        if idx is None:
            raise EngineError("MISSING_COLUMN",
                              f"数据集缺少字段 {name} 对应的列",
                              field=name, details={"row": row + 1})
        raw = self.rows[row][idx] if idx < len(self.rows[row]) else None
        if raw is None or raw == "":
            raise EngineError(
                "MISSING_VALUE",
                f"第 {row + 1} 行字段 {name} 的值为空",
                field=name, details={"row": row + 1, "column": name},
            )
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise EngineError(
                "INVALID_VALUE",
                f"第 {row + 1} 行字段 {name} 的值 {raw!r} 不是数值",
                field=name, details={"row": row + 1, "column": name},
            )
        qtype = self.analyzer.types.get(name) or _type_for_unit(unit)
        return Value(unit.to_canonical(number), qtype, unit)

    def _eval_raw_field(self, name, spec):
        unit = UNITS.get(spec.get("unit"))
        out, values = [], []
        for row in range(len(self.rows)):
            try:
                value = self._raw_value(name, row)
            except EngineError as err:
                self._row_error(err, name, row)
                out.append(None)
                values.append(None)
                continue
            out.append({"value": value.shown(), "unit": unit.symbol})
            values.append(value)
        ok = all(v is not None for v in values)
        self.row_outputs[name] = out
        self.row_values[name] = values
        self.field_reports[name] = {
            "name": name, "status": "ok" if ok else "error", "kind": "raw",
            "unit": unit.symbol if unit else spec.get("unit"),
            "rows": len(out),
            "affected_downstream": [] if ok else self.analyzer.downstream(name),
        }
        if not ok:
            self.failed.add(name)

    # ------------------------------------------------------------------
    # derived fields
    # ------------------------------------------------------------------
    def _eval_field(self, name):
        spec = self.fields[name]
        info = self.analyzer.analysis.get(name, {})
        if info.get("status") != "ok":
            self.failed.add(name)
            self.field_reports[name] = {
                "name": name, "status": "invalid",
                "reason": "分析未通过",
                "affected_downstream": self.analyzer.downstream(name),
            }
            return
        kind = spec.get("kind")
        try:
            if kind == "raw":
                self._eval_raw_field(name, spec)
            elif kind == "derived":
                self._eval_derived_field(name, spec, info)
        except EngineError as err:
            self.failed.add(name)
            err.field = err.field or name
            err.expression = err.expression or spec.get("expression")
            err.affected = self.analyzer.downstream(name)
            self.errors.append(err)
            self.field_reports[name] = {
                "name": name, "status": "error", "kind": kind,
                "expression": spec.get("expression"),
                "message": err.message, "code": err.code,
                "affected_downstream": err.affected,
            }

    def _eval_derived_field(self, name, spec, info):
        node = self.analyzer._parsed.get(name)
        qtype = self.analyzer.types[name]
        expression = spec.get("expression")
        if info.get("aggregate"):
            self._eval_aggregate_field(name, node, qtype, expression)
        else:
            self._eval_row_field(name, node, qtype, expression)

    def _eval_row_field(self, name, node, qtype, expression):
        out_rows, values, steps_by_row = [], [], {}
        row_ok, error_count = True, 0
        for row in range(len(self.rows)):
            try:
                value = self._eval(node, row, name, expression)
                shown, steps = self._to_output(value, qtype, name)
                out_rows.append({"value": shown, "unit": qtype.symbol})
                values.append(value)
                steps_by_row[row] = steps
            except EngineError as err:
                err.field = name
                err.expression = expression
                err.details.setdefault("row", row + 1)
                if error_count < MAX_ROW_ERRORS:
                    self._row_error(err, name, row)
                error_count += 1
                out_rows.append(None)
                values.append(None)
                row_ok = False
        self.row_outputs[name] = out_rows
        self.row_values[name] = values
        self._row_steps[name] = steps_by_row
        if error_count > MAX_ROW_ERRORS:
            self.errors.append(EngineError(
                "ROW_ERRORS_TRUNCATED",
                f"字段 {name} 共 {error_count} 行失败，仅显示前 {MAX_ROW_ERRORS} 条",
                field=name, expression=expression,
                affected=self.analyzer.downstream(name),
            ))
        if not row_ok:
            self.failed.add(name)
        self.field_reports[name] = {
            "name": name, "status": "ok" if row_ok else "error",
            "kind": "row", "expression": expression,
            "output_unit": qtype.symbol,
            "failed_rows": 0 if row_ok else
            sum(1 for r in out_rows if r is None),
            "affected_downstream": [] if row_ok
            else self.analyzer.downstream(name),
        }

    def _eval_aggregate_field(self, name, node, qtype, expression):
        try:
            value = self._eval(node, None, name, expression)
        except EngineError as err:
            err.field = name
            err.expression = expression
            self.failed.add(name)
            self._row_error(err, name)
            self.field_reports[name] = {
                "name": name, "status": "error", "kind": "aggregate",
                "expression": expression, "code": err.code,
                "message": err.message,
                "affected_downstream": self.analyzer.downstream(name),
            }
            return
        shown, steps = self._to_output(value, qtype, name)
        self.scalar_outputs[name] = {
            "value": shown, "unit": qtype.symbol, "steps": steps,
            "rows": len(self.rows),
        }
        self.values[name] = value
        self.field_reports[name] = {
            "name": name, "status": "ok", "kind": "aggregate",
            "expression": expression, "output_value": shown,
            "output_unit": qtype.symbol, "steps": steps,
            "affected_downstream": [],
        }

    # ------------------------------------------------------------------
    # display / conversion
    # ------------------------------------------------------------------
    def _project(self, value, target, label):
        """Express ``value`` in ``target`` units.

        Returns ``(canonical, shown, steps)``.  A bare number is unit-agnostic:
        against a dimensionless target its canonical magnitude is unchanged
        (``0.5 → 50 %``); against a dimensional target it is read as that many
        target units (``5 → 5 km``).
        """
        if value.display.symbol == target.symbol:
            return value.canonical, value.shown(), []
        if value.qtype.kind == "number":
            if all(x == 0 for x in target.dims) and target.currency is None:
                return value.canonical, value.canonical / float(target.scale), []
            shown = value.canonical
            return target.to_canonical(shown), shown, []
        shown, steps = convert(value.shown(), value.display, target, self.fx,
                               label=label)
        canonical = float(target.scale) * shown + float(target.offset)
        return canonical, shown, steps

    def _to_output(self, value, qtype, field):
        symbol = qtype.unit_symbol or qtype.symbol
        target = UNITS.get(symbol)
        if target is None:
            return value.shown(), list(value.steps)
        canonical, shown, steps = self._project(value, target, field)
        value.canonical = canonical
        value.display = target
        value.steps = list(value.steps) + steps
        return shown, value.steps

    def _coerce(self, value, other_type, other_display, field, expression):
        """Bring ``value`` to the units of the other operand of a comparison."""
        if value.qtype.kind == "number" or other_type.kind == "number":
            if value.qtype.kind == "number":
                canonical, shown, _ = self._project(value, other_display,
                                                    field)
                return canonical, shown, []
            return value.canonical, value.shown(), []
        if value.qtype.dims != other_type.dims:
            raise EngineError("UNIT_MISMATCH",
                              f"运行期量纲不一致：{value.qtype.symbol} 与 "
                              f"{other_type.symbol}",
                              field=field, expression=expression)
        fam_value = temperature_family(value.qtype.kind)
        fam_other = temperature_family(other_type.kind)
        if fam_value and fam_other and fam_value != fam_other:
            shown = _shown(value.canonical, other_display)
            return value.canonical, shown, []
        canonical, shown, steps = self._project(value, other_display, field)
        return canonical, shown, steps

    # ------------------------------------------------------------------
    # expression evaluation
    # ------------------------------------------------------------------
    def _eval(self, node, row, field, expression):
        if isinstance(node, ex.Literal):
            if node.unit is None:
                return Value(float(node.value), _number_type(), SCALAR_UNIT)
            u = node.unit
            # The literal is already expressed in ``u``; no conversion step is
            # needed because values keep their display unit as they flow.
            return Value(u.to_canonical(node.value), _type_for_unit(u), u)
        if isinstance(node, ex.Boolean):
            flag = bool(node.value)
            return Value(1.0 if flag else 0.0, _boolean_type(), SCALAR_UNIT,
                         boolean=flag)
        if isinstance(node, ex.Ref):
            return self._ref_value(node.name, row, field)
        if isinstance(node, ex.Unary):
            operand = self._eval(node.operand, row, field, expression)
            if node.op == "NOT":
                if not operand.qtype.boolean:
                    raise EngineError("TYPE_NOT_BOOLEAN", "NOT 需要布尔条件",
                                      field=field, expression=expression)
                return Value(0.0 if operand.boolean else 1.0, _boolean_type(),
                             SCALAR_UNIT, operand.steps, not operand.boolean)
            if node.op == "-":
                return Value(-operand.canonical, operand.qtype,
                             operand.display, operand.steps)
            return operand
        if isinstance(node, ex.Binary):
            return self._eval_binary(node, row, field, expression)
        if isinstance(node, ex.Compare):
            return self._eval_compare(node, row, field, expression)
        if isinstance(node, ex.BoolOp):
            flags, steps = [], []
            for item in node.values:
                value = self._eval(item, row, field, expression)
                if not value.qtype.boolean:
                    raise EngineError("TYPE_NOT_BOOLEAN", "布尔运算需要条件",
                                      field=field, expression=expression)
                flags.append(bool(value.boolean))
                steps.extend(value.steps)
            flag = all(flags) if node.op == "AND" else any(flags)
            return Value(1.0 if flag else 0.0, _boolean_type(), SCALAR_UNIT,
                         steps, flag)
        if isinstance(node, ex.If):
            cond = self._eval(node.cond, row, field, expression)
            if not cond.qtype.boolean:
                raise EngineError("CONDITION_NOT_BOOLEAN", "IF 条件必须是布尔比较",
                                  field=field, expression=expression)
            chosen = self._eval(node.then if cond.boolean else node.otherwise,
                                row, field, expression)
            chosen.steps = cond.steps + chosen.steps
            return chosen
        if isinstance(node, ex.Sum):
            return self._eval_sum(node, field, expression)
        if isinstance(node, ex.To):
            return self._eval_to(node, row, field, expression)
        raise EngineError("INTERNAL_ERROR", "未知表达式节点",
                          field=field, expression=expression)

    def _eval_to(self, node, row, field, expression):
        inner = self._eval(node.expr, row, field, expression)
        if inner.qtype.boolean:
            raise EngineError("TO_BOOLEAN", "TO 不能转换布尔值",
                              field=field, expression=expression)
        target = node.unit
        canonical, shown, steps = self._project(inner, target, field)
        return Value(canonical, _type_for_unit(target), target,
                     inner.steps + steps)

    def _eval_compare(self, node, row, field, expression):
        left = self._eval(node.left, row, field, expression)
        right = self._eval(node.right, row, field, expression)
        right_canonical, right_shown, right_steps = self._coerce(
            right, left.qtype, left.display, field, expression)
        left_shown = left.shown()
        ops = {
            "==": left.canonical == right_canonical,
            "!=": left.canonical != right_canonical,
            "<": left.canonical < right_canonical,
            "<=": left.canonical <= right_canonical,
            ">": left.canonical > right_canonical,
            ">=": left.canonical >= right_canonical,
        }
        steps = left.steps + right.steps + right_steps + [{
            "op": "compare",
            "formula": f"{_term(left_shown, left.display)} {node.op} "
                       f"{_term(right_shown, left.display)} → "
                       f"{'真' if ops[node.op] else '假'}",
            "value": 1.0 if ops[node.op] else 0.0,
            "unit": "bool",
        }]
        return Value(1.0 if ops[node.op] else 0.0, _boolean_type(),
                     SCALAR_UNIT, steps, ops[node.op])

    # ------------------------------------------------------------------
    def _ref_value(self, name, row, field):
        if name in self.failed:
            raise self._dependency_failed(name, field, row)
        spec = self.fields[name]
        qtype = self.analyzer.types.get(name)
        if spec.get("kind") == "raw":
            value = self._raw_value(name, 0 if row is None else row)
            return Value(value.canonical, value.qtype, value.display,
                         list(value.steps))
        if qtype is None:
            raise EngineError("DEPENDENCY_FAILED",
                              f"依赖字段 {name} 没有可用类型", field=field)
        if self.analyzer.analysis.get(name, {}).get("aggregate"):
            stored = self.values.get(name)
            if stored is None:
                raise EngineError("DEPENDENCY_FAILED",
                                  f"聚合字段 {name} 尚未计算", field=field)
            return Value(stored.canonical, qtype, stored.display,
                         list(stored.steps))
        if row is None:
            raise EngineError(
                "AGGREGATE_ROW_REFERENCE",
                f"聚合表达式不能直接引用逐行字段 {name}；请放入 SUM()",
                field=field, details={"reference": name},
            )
        stored = self.row_values.get(name)
        if stored is None or row >= len(stored) or stored[row] is None:
            raise EngineError("DEPENDENCY_FAILED",
                              f"第 {row + 1} 行依赖的字段 {name} 没有可用值",
                              field=field, details={"row": row + 1})
        value = stored[row]
        return Value(value.canonical, qtype, value.display, list(value.steps))

    # ------------------------------------------------------------------
    def _eval_sum(self, node, field, expression):
        total = 0.0
        display = None
        inner_type = None
        steps, sample = [], []
        for row in range(len(self.rows)):
            value = self._eval(node.expr, row, field, expression)
            if inner_type is None:
                inner_type, display = value.qtype, value.display
            total += value.canonical
            if row < 3:
                sample.append({"row": row + 1, "value": value.shown(),
                               "unit": value.display.symbol})
                steps.extend(value.steps[:2])
        if inner_type is None:
            raise EngineError("SUM_EMPTY_DATASET",
                              "数据集为空，SUM() 无法计算",
                              field=field, expression=expression)
        shown = _shown(total, display)
        steps.append({
            "op": "sum",
            "formula": f"SUM(逐行 {len(self.rows)} 行) = "
                       f"{_term(shown, display)}",
            "value": shown, "unit": display.symbol,
            "canonical": total, "dimension": inner_type.dimension_text,
        })
        result = Value(total, _copy_type(inner_type), display, steps)
        result.sample = sample
        result.rows = len(self.rows)
        return result

    # ------------------------------------------------------------------
    def _eval_binary(self, node, row, field, expression):
        left = self._eval(node.left, row, field, expression)
        right = self._eval(node.right, row, field, expression)
        if node.op in ("+", "-"):
            return self._eval_additive(node, left, right, field, expression)
        if node.op in ("*", "/"):
            return self._eval_multiplicative(node, left, right, row, field,
                                             expression)
        if node.op == "^":
            return self._eval_power(node, left, field, expression)
        raise EngineError("INTERNAL_ERROR", f"未知运算符 {node.op}",
                          field=field, expression=expression)

    def _eval_power(self, node, left, field, expression):
        exponent = ex.const_number(node.right)
        if exponent is None:
            raise EngineError("POWER_NOT_CONSTANT", "指数必须是常量",
                              field=field, expression=expression)
        value = left.canonical ** exponent
        if exponent == 1:
            return Value(value, left.qtype, left.display, left.steps)
        dims = tuple(x * int(exponent) for x in left.qtype.dims)
        out_type = QType(dims, "quantity")
        display = _canonical_unit(dims, "quantity")
        lv = left.shown()
        steps = left.steps + [{
            "op": "^",
            "formula": f"({_term(lv, left.display)}) ^ {fmt(exponent)} = "
                       f"{_term(value, display)}",
            "value": value, "unit": display.symbol,
            "canonical": value, "dimension": out_type.dimension_text,
        }]
        return Value(value, out_type, display, steps)

    def _eval_additive(self, node, left, right, field, expression):
        lf = temperature_family(left.qtype.kind)
        rf = temperature_family(right.qtype.kind)
        right_steps = []
        left_shown = left.shown()
        left_unit = left.display
        if lf == "absolute" and rf == "absolute":
            # Difference of two readings: show both in the left scale and
            # label the result as a temperature *difference*.
            right_canonical, right_shown, st = self._project(
                right, left.display, field)
            right_steps = st
            right_unit = left.display
            display = _delta_unit(left.display)
            out_type = QType(left.qtype.dims, "temperature_delta",
                             unit_symbol=display.symbol)
        elif lf == "absolute" and rf == "delta":
            delta_display = _delta_unit(left.display)
            right_canonical, right_shown, st = self._project(
                right, delta_display, field)
            right_steps = st
            right_unit = delta_display
            display = left.display
            out_type = _copy_type(left.qtype)
        elif lf == "delta" and rf == "absolute":
            delta_display = _delta_unit(right.display)
            left_canonical, left_shown, st = self._project(
                left, delta_display, field)
            right_steps = st
            left_unit = delta_display
            right_canonical, right_shown = right.canonical, right.shown()
            right_unit = right.display
            display = right.display
            out_type = _copy_type(right.qtype)
            left = Value(left_canonical, left.qtype, left_unit, left.steps)
        elif lf == "delta" and rf == "delta":
            right_canonical, right_shown, st = self._project(
                right, left.display, field)
            right_steps = st
            right_unit = left.display
            display = left.display
            out_type = _copy_type(left.qtype)
        else:
            right_canonical, right_shown, st = self._coerce(
                right, left.qtype, left.display, field, expression)
            right_steps = st
            right_unit = left.display
            display = left.display
            if left.qtype.kind == "number" and right.qtype.kind != "number":
                # A bare constant adopts the typed operand's unit, so the
                # printed arithmetic is numerically true (1 vs 90 %).
                display = right.display
                left_shown = _shown(left.canonical, display)
                left_unit = display
                right_shown, right_unit = right.shown(), right.display
                out_type = _copy_type(right.qtype)
            elif right.qtype.kind == "number" and left.qtype.kind != "number":
                display = left.display
                right_shown = _shown(right.canonical, display)
                right_unit = display
                out_type = _copy_type(left.qtype)
            else:
                out_type = _copy_type(left.qtype)
        value = (left.canonical + right_canonical if node.op == "+"
                 else left.canonical - right_canonical)
        out = _shown(value, display)
        steps = left.steps + right.steps + right_steps + [{
            "op": node.op,
            "formula": f"{_term(left_shown, left_unit)} {node.op} "
                       f"{_term(right_shown, right_unit)} = "
                       f"{_term(out, display)}",
            "value": out, "unit": display.symbol,
            "canonical": value, "dimension": out_type.dimension_text,
            "inputs": [{"value": left_shown, "unit": left_unit.symbol},
                       {"value": right_shown, "unit": right_unit.symbol}],
            "result": {"value": out, "unit": display.symbol},
        }]
        return Value(value, out_type, display, steps)

    def _eval_multiplicative(self, node, left, right, row, field, expression):
        if node.op == "/" and right.canonical == 0:
            where = (f"第 {row + 1} 行分母为零"
                     if row is not None else "聚合表达式分母为零")
            raise EngineError(
                "DIVISION_BY_ZERO",
                f"{where}（{ex.expression_text(node.right)}），无法计算 {field}",
                field=field, expression=expression,
                details={"row": None if row is None else row + 1,
                         "denominator": ex.expression_text(node.right)},
            )
        value = (left.canonical * right.canonical if node.op == "*"
                 else left.canonical / right.canonical)
        out_type = _product_type(left.qtype, right.qtype, node.op)
        display, exact = composite_unit(left.display, right.display,
                                        node.op, out_type)
        pre_steps = []
        left_unit, right_unit = left.display, right.display
        left_shown, right_shown = left.shown(), right.shown()
        if not exact:
            # No catalogue unit at the combined scale.  Fall back to canonical
            # (base-unit) numbers for both operands so every printed number
            # keeps its true unit, and record the rescaling explicitly.
            display = _canonical_unit(out_type.dims, out_type.kind,
                                      out_type.currency)
            left_unit = _canonical_unit(left.display.dims, left.qtype.kind,
                                        left.qtype.currency)
            right_unit = _canonical_unit(right.display.dims, right.qtype.kind,
                                         right.qtype.currency)
            for operand, unit in ((left, left_unit), (right, right_unit)):
                shown, st = convert(operand.shown(), operand.display, unit,
                                    self.fx, label=field)
                if shown != operand.shown() or st:
                    operand_shown = shown
                else:
                    operand_shown = operand.shown()
                if operand is left:
                    left_shown = operand_shown
                else:
                    right_shown = operand_shown
                pre_steps.extend(st)
            out = value
        else:
            out = _shown(value, display)
        steps = left.steps + right.steps + pre_steps + [{
            "op": node.op,
            "formula": f"{_term(left_shown, left_unit)} "
                       f"{OP_SYMBOLS[node.op]} "
                       f"{_term(right_shown, right_unit)} = "
                       f"{_term(out, display)}",
            "value": out, "unit": display.symbol,
            "canonical": value, "dimension": out_type.dimension_text,
            "inputs": [{"value": left_shown, "unit": left_unit.symbol},
                       {"value": right_shown, "unit": right_unit.symbol}],
            "result": {"value": out, "unit": display.symbol},
        }]
        return Value(value, out_type, display, steps)

    # ------------------------------------------------------------------
    def _build_reports(self):
        self.report = {
            "fields": [self.analyzer.analysis.get(f["name"], {})
                       for f in self.ruleset.get("fields", [])],
            "errors": [e.to_dict() for e in self.analyzer.errors] +
                      [e.to_dict() for e in self.errors],
            "rows": len(self.rows),
            "ok": not self.analyzer.errors and not self.errors,
        }


def evaluate(ruleset, dataset, fx=None):
    analyzer = analyze(ruleset, fx)
    ev = Evaluator(ruleset, dataset, fx, analyzer)
    return ev.run(), analyzer
