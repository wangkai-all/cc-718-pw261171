"""Static analysis of derived-field rules.

The analyzer is the gate in front of publishing.  It infers a full unit type
for every expression, checks dimensional compatibility, temperatures, money
and aggregation rules, detects dependency cycles, and reports every problem
against the exact expression plus all downstream fields it blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import expressions as ex
from .errors import EngineError
from .units import (UNITS, DIM_NAMES, Unit, canonical_symbol, dimension_label,
                    fmt, linear_factor, temperature_family)

KIND_LABELS = {
    "number": "标量",
    "ratio": "比例",
    "count": "数量",
    "money": "货币",
    "temperature": "绝对温度",
    "temperature_abs": "绝对温度",
    "temperature_delta": "温差",
    "quantity": "物理量",
    "boolean": "布尔",
    "unknown": "未知",
}


@dataclass
class QType:
    dims: tuple
    kind: str = "quantity"
    currency: str | None = None
    unit_symbol: str | None = None
    unknown: bool = False
    boolean: bool = False
    detail: str = ""

    @property
    def label(self):
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def symbol(self):
        if self.unit_symbol:
            return self.unit_symbol
        if self.boolean:
            return "bool"
        if self.unknown:
            return "?"
        return canonical_symbol(self.dims, self.kind, self.currency)

    @property
    def dimension_text(self):
        if self.boolean:
            return "布尔"
        if self.unknown:
            return "未知"
        return dimension_label(self.dims, self.currency)

    def as_unit(self):
        """Canonical unit for this type.

        Engine values are stored canonically (SI base units, K, fraction),
        so conversions must start from a scale-1 / offset-0 unit.  The
        human-facing unit is ``unit_symbol``.
        """
        return Unit(self.dims, 1, 0, self.kind,
                    canonical_symbol(self.dims, self.kind, self.currency),
                    self.currency)

    def to_dict(self):
        return {
            "kind": self.kind,
            "kind_label": self.label,
            "dimension": self.dimension_text,
            "symbol": self.symbol,
            "currency": self.currency,
            "unknown": self.unknown,
            "boolean": self.boolean,
            "detail": self.detail,
        }


def number_type():
    return QType((0,) * len(DIM_NAMES), "number", unit_symbol="1")


def unknown_type(detail=""):
    return QType((0,) * len(DIM_NAMES), "unknown", unknown=True, detail=detail)


def boolean_type():
    return QType((0,) * len(DIM_NAMES), "boolean", boolean=True,
                 unit_symbol="bool")


def dims_zero(dims):
    return all(x == 0 for x in dims)


def _err(code, message, *, field=None, expression=None, path=None, details=None):
    return EngineError(code, message, field=field, expression=expression,
                       path=path, details=details)


class Analyzer:
    def __init__(self, ruleset, fx=None):
        self.ruleset = ruleset
        self.fx = fx or {}
        self.fields = {f["name"]: f for f in ruleset.get("fields", [])}
        self.order = []
        self.types = {}          # name -> QType
        self.analysis = {}       # name -> dict
        self.errors = []         # list[EngineError]
        self.dependencies = {}   # derived name -> set of derived deps
        self._dependents = {}
        self._parsed = {}
        self._cycle_fields = set()
        self._aggregate_outside = {}
        self.aggregate = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def add_error(self, err, affected=None):
        err.affected = affected if affected is not None else self.downstream(err.field)
        self.errors.append(err)

    def downstream(self, field_name, include_self=False):
        if not field_name:
            return []
        out = []
        seen = set()
        stack = list(self._dependents.get(field_name, ()))
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
            stack.extend(self._dependents.get(name, ()))
        if include_self:
            out.insert(0, field_name)
        return out

    def field_unit(self, spec):
        raw = spec.get("unit")
        if raw is None or str(raw).strip() in ("", "?", "unknown", "未知"):
            return None, True
        if raw not in UNITS:
            return None, False
        return UNITS[raw], False

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------
    def run(self):
        fields = self.ruleset.get("fields", [])
        seen = set()
        for spec in fields:
            name = spec.get("name", "")
            if not name:
                self.add_error(_err("FIELD_NAME_MISSING", "字段缺少名称"), [])
                continue
            if name in seen:
                self.add_error(_err("DUPLICATE_FIELD", f"字段 {name} 重复定义",
                                    field=name), [])
            seen.add(name)
            if spec.get("kind") not in ("raw", "derived"):
                self.add_error(_err("FIELD_KIND_INVALID",
                                    f"字段 {name} 的 kind 必须是 raw 或 derived",
                                    field=name), [])
        self._build_graph()
        self._resolve_aggregates()
        self._detect_cycles()
        self._analyze_raw_fields()
        self._analyze_all()
        return self

    def _analyze_raw_fields(self):
        for name, spec in self.fields.items():
            if spec.get("kind") != "raw":
                continue
            unit, unknown = self.field_unit(spec)
            if unit is None:
                err = _err("FIELD_UNIT_UNKNOWN",
                           f"原字段 {name} 的单位"
                           + ("未填写" if unknown else
                              f" {spec.get('unit')!r} 未登记")
                           + "；未知单位不能参与派生计算",
                           field=name,
                           details={"declared_unit": spec.get("unit")})
                self.add_error(err)
                self.analysis[name] = {"name": name, "status": "error",
                                       "type": None, "kind": "raw",
                                       "error": err.to_dict()}
                continue
            qtype = QType(unit.dims, unit.kind, currency=unit.currency,
                          unit_symbol=unit.symbol)
            if unit.kind == "money" and unit.currency:
                qtype.kind = "money"
            self.types[name] = qtype
            self.analysis[name] = {
                "name": name, "status": "ok", "type": qtype.to_dict(),
                "kind": "raw", "unit": unit.symbol,
                "references": [],
            }

    # ------------------------------------------------------------------
    # graph
    # ------------------------------------------------------------------
    def _build_graph(self):
        derived_names = {n for n, f in self.fields.items()
                         if f.get("kind") == "derived"}
        for name in derived_names:
            self.dependencies[name] = set()
        for name in derived_names:
            spec = self.fields[name]
            text = (spec.get("expression") or "").strip()
            if not text:
                self.add_error(_err("EMPTY_EXPRESSION", f"派生字段 {name} 的表达式为空",
                                    field=name, expression=text), [])
                continue
            try:
                node = ex.parse(text)
            except EngineError as err:
                err.field = name
                err.expression = text
                self.add_error(err)
                continue
            self._parsed[name] = node
            refs = node.refs()
            for ref in refs:
                if ref not in self.fields:
                    self.add_error(_err(
                        "UNKNOWN_REFERENCE",
                        f"字段 {name} 引用了不存在的字段 {ref}",
                        field=name, expression=text,
                        details={"reference": ref},
                    ))
                elif ref in derived_names:
                    self.dependencies[name].add(ref)
                elif self.fields[ref].get("kind") != "raw":
                    self.add_error(_err(
                        "INVALID_REFERENCE", f"{ref} 不是可引用字段",
                        field=name, expression=text))
                # Every reference (raw or derived) makes ``name`` a
                # dependent, so a broken upstream can list its real
                # downstream impact.
                self._dependents.setdefault(ref, set()).add(name)
            if ex.contains_sum(node):
                sums = ex.sum_nodes(node)
                if any(ex.contains_sum(s.expr) for s in sums):
                    self.add_error(_err(
                        "NESTED_SUM", f"字段 {name} 出现嵌套 SUM()",
                        field=name, expression=text))
                outside = ex.refs_outside_sum(node)
                if outside:
                    self._aggregate_outside[name] = outside
        for name, deps in self.dependencies.items():
            for dep in deps:
                self._dependents.setdefault(dep, set()).add(name)

    def _resolve_aggregates(self):
        """"Aggregate" fields produce one value per dataset, not one per row."""
        for name, node in self._parsed.items():
            self.aggregate[name] = ex.contains_sum(node)
        changed = True
        while changed:
            changed = False
            for name in self._parsed:
                if self.aggregate.get(name):
                    continue
                if any(self.aggregate.get(dep) for dep in
                       self.dependencies.get(name, ())):
                    self.aggregate[name] = True
                    changed = True
        for name, outside in self._aggregate_outside.items():
            bad = sorted(r for r in outside
                         if r in self.fields and
                         (self.fields[r].get("kind") == "raw" or
                          not self.aggregate.get(r)))
            if bad:
                self.add_error(_err(
                    "AGGREGATE_MIXED",
                    f"字段 {name} 的聚合表达式在 SUM() 之外引用了逐行字段 "
                    + "、".join(bad) + "；请把这些字段放进 SUM()",
                    field=name, expression=self.fields[name].get("expression"),
                    details={"references": bad}))

    def _detect_cycles(self):
        state = {}
        stack = []

        def visit(name):
            state[name] = 1
            stack.append(name)
            for dep in sorted(self.dependencies.get(name, ())):
                if state.get(dep, 0) == 1:
                    idx = stack.index(dep)
                    cycle = stack[idx:] + [dep]
                    self._cycle_fields.update(stack[idx:])
                    self.add_error(_err(
                        "DEPENDENCY_CYCLE",
                        "派生字段存在循环依赖：" + " → ".join(cycle),
                        field=dep,
                        expression=self.fields[dep].get("expression"),
                        details={"cycle": cycle},
                    ), sorted(set(stack[idx:])))
                elif state.get(dep, 0) == 0:
                    visit(dep)
            stack.pop()
            state[name] = 2

        for name in sorted(self.dependencies):
            if state.get(name, 0) == 0:
                visit(name)

    # ------------------------------------------------------------------
    # analysis
    # ------------------------------------------------------------------
    def _analyze_all(self):
        remaining = dict(self.dependencies)
        analysed = []
        while remaining:
            progress = False
            for name in sorted(remaining):
                deps = remaining[name]
                if deps <= set(analysed):
                    self._analyze_field(name)
                    analysed.append(name)
                    progress = True
            for name in list(remaining):
                if name in analysed:
                    remaining.pop(name)
            if not progress:
                for name in remaining:
                    if name not in self._cycle_fields:
                        self.analysis[name] = {"name": name, "status": "blocked",
                                               "type": None,
                                               "blocked_by": sorted(remaining[name])}
                break
        self.order = [n for n in analysed if self.analysis.get(n, {}).get("status") == "ok"]

    def _analyze_field(self, name):
        spec = self.fields[name]
        node = self._parsed.get(name)
        text = spec.get("expression")
        deps = self.dependencies.get(name, set())
        failed = [d for d in deps if self.analysis.get(d, {}).get("status") != "ok"]
        if failed:
            self.analysis[name] = {
                "name": name, "status": "blocked", "type": None,
                "blocked_by": sorted(failed),
                "expression": text,
                "aggregate": bool(self.aggregate.get(name)),
            }
            return
        if node is None:
            self.analysis[name] = {"name": name, "status": "error", "type": None,
                                   "expression": text}
            return
        try:
            qtype = self.infer(node, name, text)
            out_sym = spec.get("output_unit")
            if out_sym:
                if out_sym not in UNITS:
                    raise _err("UNKNOWN_OUTPUT_UNIT",
                               f"输出单位 {out_sym!r} 未登记",
                               field=name, expression=text,
                               details={"symbol": out_sym})
                target = UNITS[out_sym]
                self._check_convertible(qtype, target, name, text,
                                        "OUTPUT_UNIT_MISMATCH")
                if (temperature_family(qtype.kind) or temperature_family(target.kind)) and \
                        temperature_family(qtype.kind) != temperature_family(target.kind):
                    raise _err("TEMPERATURE_KIND_MISMATCH",
                               f"输出单位 {out_sym} 与推导的 {qtype.label} 不匹配",
                               field=name, expression=text)
                if qtype.kind == "money" and target.currency and \
                        qtype.currency and target.currency != qtype.currency:
                    if not self.fx.get(f"{qtype.currency}>{target.currency}") and \
                            not self.fx.get(f"{target.currency}>{qtype.currency}"):
                        raise _err("CURRENCY_RATE_MISSING",
                                   f"缺少 {qtype.currency}→{target.currency} 汇率，"
                                   f"不能按 {out_sym} 输出",
                                   field=name, expression=text)
                qtype.unit_symbol = out_sym
            self.types[name] = qtype
            self.analysis[name] = {
                "name": name,
                "status": "ok",
                "type": qtype.to_dict(),
                "expression": text,
                "output_unit": qtype.symbol,
                "references": sorted(node.refs()),
                "aggregate": bool(self.aggregate.get(name)),
            }
        except EngineError as err:
            err.field = err.field or name
            err.expression = err.expression or text
            self.add_error(err)
            self.analysis[name] = {"name": name, "status": "error",
                                   "type": None, "expression": text,
                                   "aggregate": bool(self.aggregate.get(name)),
                                   "error": err.to_dict()}

    # ------------------------------------------------------------------
    # type inference
    # ------------------------------------------------------------------
    def infer(self, node, field, text, inside_sum=False):
        if isinstance(node, ex.Literal):
            if node.unit is None:
                return number_type()
            u = node.unit
            if u.kind == "money" and u.currency:
                return QType(u.dims, "money", currency=u.currency,
                             unit_symbol=u.symbol)
            return QType(u.dims, u.kind, currency=u.currency,
                         unit_symbol=u.symbol)
        if isinstance(node, ex.Boolean):
            return boolean_type()
        if isinstance(node, ex.Ref):
            spec = self.fields.get(node.name)
            if spec is None:
                raise _err("UNKNOWN_REFERENCE", f"引用了不存在的字段 {node.name}",
                           field=field, expression=text,
                           details={"reference": node.name})
            if spec.get("kind") == "derived":
                t = self.types.get(node.name)
                if t is None:
                    raise _err("DEPENDENCY_FAILED",
                               f"依赖的派生字段 {node.name} 无法确定单位",
                               field=field, expression=text)
                return QType(t.dims, t.kind, t.currency, t.unit_symbol,
                             t.unknown, t.boolean, t.detail)
            u, unknown = self.field_unit(spec)
            if u is None:
                who = node.name
                raise _err(
                    "FIELD_UNIT_UNKNOWN",
                    f"字段 {who} 的原单位"
                    + ("未填写" if unknown else "未登记")
                    + "；未知单位不能当作无量纲参与运算",
                    field=field, expression=text,
                    details={"reference": who, "declared_unit": spec.get("unit")},
                )
            kind = u.kind
            if kind == "money" and u.currency:
                return QType(u.dims, "money", currency=u.currency,
                             unit_symbol=u.symbol)
            return QType(u.dims, kind, currency=u.currency, unit_symbol=u.symbol)
        if isinstance(node, ex.Unary):
            operand = self.infer(node.operand, field, text, inside_sum)
            if node.op == "NOT":
                if not operand.boolean:
                    raise _err("TYPE_NOT_BOOLEAN", "NOT 只能作用于布尔条件",
                               field=field, expression=text)
                return boolean_type()
            if operand.boolean:
                raise _err("TYPE_NOT_NUMERIC", "数值运算不能使用布尔值",
                           field=field, expression=text)
            self._reject_unknown(operand, field, text, node)
            return operand
        if isinstance(node, ex.Binary):
            return self._infer_binary(node, field, text, inside_sum)
        if isinstance(node, ex.Compare):
            left = self.infer(node.left, field, text, inside_sum)
            right = self.infer(node.right, field, text, inside_sum)
            self._unify(left, right, field, text, node, "COMPARISON_UNIT_MISMATCH",
                        comparing=True)
            return boolean_type()
        if isinstance(node, ex.BoolOp):
            for value in node.values:
                t = self.infer(value, field, text, inside_sum)
                if not t.boolean:
                    raise _err("TYPE_NOT_BOOLEAN",
                               f"{node.op} 的操作数必须是布尔条件",
                               field=field, expression=text)
            return boolean_type()
        if isinstance(node, ex.If):
            cond = self.infer(node.cond, field, text, inside_sum)
            if not cond.boolean:
                raise _err("CONDITION_NOT_BOOLEAN", "IF 的条件必须是布尔比较",
                           field=field, expression=text)
            then = self.infer(node.then, field, text, inside_sum)
            other = self.infer(node.otherwise, field, text, inside_sum)
            if then.boolean or other.boolean:
                if then.boolean != other.boolean:
                    raise _err("BRANCH_TYPE_MISMATCH",
                               "IF 两个分支一个是布尔值、一个不是",
                               field=field, expression=text)
                return boolean_type()
            return self._unify(then, other, field, text, node,
                               "BRANCH_UNIT_MISMATCH", branch=True)
        if isinstance(node, ex.Sum):
            if inside_sum:
                raise _err("NESTED_SUM", "SUM() 不能嵌套",
                           field=field, expression=text)
            inner = self.infer(node.expr, field, text, inside_sum=True)
            if inner.boolean:
                raise _err("SUM_BOOLEAN", "SUM() 不能对布尔值求和",
                           field=field, expression=text)
            self._reject_unknown(inner, field, text, node)
            return QType(inner.dims, inner.kind, inner.currency,
                         inner.unit_symbol, inner.unknown, False,
                         detail="聚合")
        if isinstance(node, ex.To):
            value = self.infer(node.expr, field, text, inside_sum)
            if value.boolean:
                raise _err("TO_BOOLEAN", "TO 不能转换布尔值",
                           field=field, expression=text)
            self._reject_unknown(value, field, text, node)
            target = node.unit
            self._check_convertible(value, target, field, text,
                                    "CONVERSION_MISMATCH")
            if (temperature_family(value.kind) or temperature_family(target.kind)) and \
                    temperature_family(value.kind) != temperature_family(target.kind):
                raise _err(
                    "TEMPERATURE_KIND_MISMATCH",
                    f"TO 目标 {target.symbol} 的温标类型与表达式推导结果"
                    f"（{value.label}）不一致",
                    field=field, expression=text,
                    details={"from": value.symbol, "to": target.symbol},
                )
            if value.kind == "money" and target.currency and \
                    value.currency and target.currency != value.currency:
                if linear_factor(value.as_unit(), target, self.fx) is None:
                    raise _err(
                        "CURRENCY_RATE_MISSING",
                        f"缺少 {value.currency}→{target.currency} 汇率，不能 TO({target.symbol})",
                        field=field, expression=text,
                        details={"from": value.currency, "to": target.currency},
                    )
            return QType(target.dims, target.kind, target.currency,
                         unit_symbol=target.symbol)
        raise _err("INTERNAL_ERROR", "未知表达式节点",
                   field=field, expression=text)

    def _reject_unknown(self, qtype, field, text, node):
        if qtype.unknown:
            raise _err(
                "FIELD_UNIT_UNKNOWN",
                f"表达式包含单位未知的值（{qtype.detail or '未知字段'}）；"
                "未知单位不能当作无量纲",
                field=field, expression=text,
                details={"detail": qtype.detail},
            )

    def _infer_binary(self, node, field, text, inside_sum):
        op = node.op
        left = self.infer(node.left, field, text, inside_sum)
        right = self.infer(node.right, field, text, inside_sum)
        if left.boolean or right.boolean:
            raise _err("TYPE_NOT_NUMERIC", f"{op} 不能用于布尔值",
                       field=field, expression=text)
        self._reject_unknown(left, field, text, node)
        self._reject_unknown(right, field, text, node)
        if op in ("+", "-"):
            return self._infer_additive(op, left, right, field, text, node)
        if op in ("*", "/"):
            return self._infer_multiply(op, left, right, field, text, node)
        if op == "^":
            return self._infer_power(left, right, field, text, node)
        raise _err("INTERNAL_ERROR", f"未知运算符 {op}", field=field, expression=text)

    # -- additive ------------------------------------------------------
    def _infer_additive(self, op, left, right, field, text, node):
        if left.dims != right.dims:
            raise _err(
                "UNIT_MISMATCH",
                f"加减法两侧量纲不同：{left.symbol}（{left.dimension_text}）"
                f" 与 {right.symbol}（{right.dimension_text}）",
                field=field, expression=text,
                details={"left": left.to_dict(), "right": right.to_dict(),
                         "operator": op},
            )
        lk, rk = left.kind, right.kind
        lf, rf = temperature_family(lk), temperature_family(rk)
        if lf or rf:
            if lf is None or rf is None:
                bad = left if lf is not None else right
                raise _err(
                    "TEMPERATURE_KIND_MISMATCH",
                    f"绝对温度/温差 {bad.symbol} 不能与普通数值相加；"
                    "请使用温差字段或先 TO(..., \"K\")",
                    field=field, expression=text,
                    details={"left": left.symbol, "right": right.symbol},
                )
            if lf == "absolute" and rf == "absolute":
                if op == "+":
                    raise _err(
                        "AFFINE_TEMPERATURE_ADD",
                        "两个绝对温度不能相加；如需温度差请改用减法，"
                        "或先 TO(..., \"K\") 后明确表达意图",
                        field=field, expression=text,
                        details={"left": left.symbol, "right": right.symbol},
                    )
                return QType(left.dims, "temperature_delta",
                             unit_symbol="Δ°C" if left.unit_symbol in ("°C", "°F")
                             else "ΔK")
            if lf == "absolute" or rf == "absolute":
                result_left = lf == "absolute"
                if op == "+":
                    src = left if result_left else right
                    return QType(src.dims, "temperature_abs", currency=None,
                                 unit_symbol=src.unit_symbol)
                # absolute - delta keeps absolute; delta - absolute invalid
                if lf == "absolute":
                    return QType(left.dims, "temperature_abs", currency=None,
                                 unit_symbol=left.unit_symbol)
                raise _err(
                    "TEMPERATURE_KIND_MISMATCH",
                    f"不能从温差 {left.symbol} 中减去绝对温度 {right.symbol}",
                    field=field, expression=text,
                )
            return QType(left.dims, "temperature_delta",
                         unit_symbol=left.unit_symbol or "Δ°C")
        if lk == "money" and rk == "money":
            if left.currency == right.currency:
                cur = left.currency
            else:
                if linear_factor(left.as_unit(), right.as_unit(), self.fx) is None:
                    raise _err(
                        "CURRENCY_RATE_MISSING",
                        f"不能直接相{ '加' if op == '+' else '减' } "
                        f"{left.symbol} 与 {right.symbol}：缺少 "
                        f"{left.currency}→{right.currency} 汇率",
                        field=field, expression=text,
                        details={"from": left.currency, "to": right.currency},
                    )
                cur = left.currency
            return QType(left.dims, "money", currency=cur,
                         unit_symbol=left.currency)
        if lk == "money" or rk == "money":
            raise _err("MONEY_UNIT_MISMATCH",
                       "货币只能与同量纲货币相加；价格需先乘以数量得到金额",
                       field=field, expression=text,
                       details={"left": left.symbol, "right": right.symbol})
        if lk != rk:
            # number / ratio are both dimensionless but typed; permit
            # number+ratio only by promoting to ratio.
            if {lk, rk} == {"number", "ratio"}:
                return QType(left.dims, "ratio", unit_symbol="ratio")
            raise _err(
                "KIND_MISMATCH",
                f"类型不同不能相加：{left.label}({left.symbol}) 与 "
                f"{right.label}({right.symbol})",
                field=field, expression=text,
                details={"left": lk, "right": rk},
            )
        if lk == "ratio" and op == "-" and left.dims == right.dims:
            return QType(left.dims, "ratio", unit_symbol="ratio")
        return QType(left.dims, lk, currency=left.currency or right.currency,
                     unit_symbol=left.unit_symbol or right.unit_symbol)

    # -- multiplicative -------------------------------------------------
    def _infer_multiply(self, op, left, right, field, text, node):
        # Only *affine* scales (°C / °F) are forbidden in products.  Kelvin
        # is an absolute scale with no offset, so after ``TO(..., "K")`` a
        # temperature may take part in multiplication.  Temperature deltas
        # have no offset and are allowed as well.
        affine = [t for t in (left, right) if t.kind == "temperature"
                  and t.unit_symbol in ("°C", "°F")]
        if affine:
            bad = affine[0]
            raise _err(
                "AFFINE_TEMPERATURE_MULTIPLY",
                f"绝对温度 {bad.symbol} 带偏移，不能直接参与乘除；"
                "请先 TO(..., \"K\") 转为绝对温标，或使用温差字段",
                field=field, expression=text,
                details={"affine_operator": op, "unit": bad.symbol},
            )
        # money x money is meaningless (currency²)
        money_kinds = sum(1 for t in (left, right) if t.kind == "money")
        if money_kinds == 2:
            raise _err(
                "MONEY_PRODUCT",
                f"不能把金额与金额相乘（{left.symbol} × {right.symbol}）",
                field=field, expression=text,
            )
        dims = tuple(a + b if op == "*" else a - b
                     for a, b in zip(left.dims, right.dims))
        if op == "/" and right.kind == "money":
            raise _err("MONEY_DENOMINATOR",
                       "货币不能作为分母；请先换算成同币种金额再相除得到比例",
                       field=field, expression=text)
        if op == "/" and ex.const_number(node.right) == 0:
            raise _err("DIVISION_BY_ZERO",
                       "分母为常量零，表达式无法计算",
                       field=field, expression=text)
        # derive kind
        if left.kind == "money":
            kind, currency = "money", left.currency
            if right.kind == "money":
                kind, currency = "ratio", None
        elif right.kind == "money":
            kind, currency = "money", right.currency
        elif op == "/" and dims_zero(dims):
            if left.kind == "count" and right.kind == "count":
                kind, currency = "ratio", None
            elif left.kind == right.kind == "ratio":
                kind, currency = "ratio", None
            elif left.kind == "number" and right.kind == "number":
                kind, currency = "number", None
            else:
                kind, currency = "ratio", None
        elif left.kind == "ratio" and right.kind in ("number", "ratio"):
            kind, currency = right.kind, None
            if right.kind == "ratio" and op == "*":
                kind = "ratio"
        elif right.kind == "ratio" and left.kind in ("number", "ratio"):
            kind, currency = left.kind, None
            if left.kind == "ratio" and op == "*":
                kind = "ratio"
        elif left.kind == "number":
            kind, currency = right.kind, right.currency
        elif right.kind == "number":
            kind, currency = left.kind, left.currency
        elif left.kind == "count" or right.kind == "count":
            kind, currency = "count", None
            if op == "/" and left.kind == "count" and right.kind == "count":
                kind = "ratio"
            if op == "/" and right.kind == "count":
                kind = "quantity"
        else:
            kind, currency = "quantity", None
        symbol = None
        if kind == "money" and dims_zero(dims):
            symbol = currency
        elif kind == "ratio":
            symbol = "ratio"
        return QType(dims, kind, currency=currency, unit_symbol=symbol)

    def _infer_power(self, base, exponent, field, text, node):
        power = ex.const_number(node.right)
        if power is None:
            raise _err("POWER_NOT_CONSTANT", "指数必须是常量数值",
                       field=field, expression=text)
        if power != int(power):
            raise _err("POWER_NOT_INTEGER", "仅支持整数次幂",
                       field=field, expression=text)
        power = int(power)
        if base.kind == "temperature" and base.unit_symbol in ("°C", "°F"):
            raise _err("AFFINE_TEMPERATURE_MULTIPLY",
                       "绝对温度不能参与幂运算；请先转换为绝对温标",
                       field=field, expression=text)
        if base.kind == "money":
            raise _err("MONEY_POWER", "货币不能参与幂运算",
                       field=field, expression=text)
        if base.kind == "count" and power != 1:
            raise _err("COUNT_POWER", "数量字段不能参与幂运算",
                       field=field, expression=text)
        dims = tuple(x * power for x in base.dims)
        if power == 1:
            return QType(dims, base.kind, base.currency, base.unit_symbol)
        return QType(dims, "quantity")

    # -- unification ----------------------------------------------------
    def _unify(self, left, right, field, text, node, code, *, comparing=False,
               branch=False):
        self._reject_unknown(left, field, text, node)
        self._reject_unknown(right, field, text, node)
        if left.boolean or right.boolean:
            if left.boolean and right.boolean:
                return boolean_type()
            raise _err(code, "布尔值不能与数值比较", field=field, expression=text)
        lk, rk = left.kind, right.kind
        if left.dims != right.dims:
            # A bare numeric literal (kind "number", zero dimension) is a
            # unit-agnostic constant: ``sessions > 0`` or ``ELSE 0``.
            if lk == "number" and not left.boolean:
                if branch:
                    return QType(right.dims, rk, currency=right.currency,
                                 unit_symbol=right.unit_symbol)
                return boolean_type()
            if rk == "number" and not right.boolean:
                if branch:
                    return QType(left.dims, lk, currency=left.currency,
                                 unit_symbol=left.unit_symbol)
                return boolean_type()
            raise _err(
                code,
                ("IF 两个分支" if branch else "比较两侧")
                + f"单位不兼容：{left.symbol}（{left.dimension_text}）与 "
                f"{right.symbol}（{right.dimension_text}）",
                field=field, expression=text,
                details={"left": left.to_dict(), "right": right.to_dict()},
            )
        if lk != rk:
            if {lk, rk} == {"number", "ratio"}:
                return QType(left.dims, "ratio", unit_symbol="ratio")
            if lk == "money" or rk == "money":
                raise _err(
                    "MONEY_UNIT_MISMATCH",
                    f"金额 {left.symbol} 与 {right.symbol} 类型不同",
                    field=field, expression=text)
            raise _err(
                code,
                ("IF 两个分支" if branch else "比较两侧")
                + f"类型不同：{left.label}({left.symbol}) 与 "
                f"{right.label}({right.symbol})",
                field=field, expression=text,
                details={"left": lk, "right": rk},
            )
        if temperature_family(lk) and temperature_family(lk) == temperature_family(rk):
            if branch:
                return QType(left.dims, lk, currency=None,
                             unit_symbol=left.unit_symbol or right.unit_symbol)
            return boolean_type()
        if temperature_family(lk) != temperature_family(rk) and \
                (temperature_family(lk) or temperature_family(rk)):
            raise _err(code, "绝对温度与温差不能直接比较或合并",
                       field=field, expression=text)
        if lk == "money" and left.currency != right.currency:
            if linear_factor(left.as_unit(), right.as_unit(), self.fx) is None:
                raise _err(
                    "CURRENCY_RATE_MISSING",
                    f"不能直接比较/合并 {left.symbol} 与 {right.symbol}：缺少 "
                    f"{left.currency}→{right.currency} 汇率",
                    field=field, expression=text,
                    details={"from": left.currency, "to": right.currency},
                )
        if branch:
            return QType(left.dims, lk, currency=left.currency or right.currency,
                         unit_symbol=left.unit_symbol or right.unit_symbol)
        return boolean_type()

    def _check_convertible(self, qtype, target, field, text, code):
        if qtype.boolean:
            raise _err(code, "布尔值没有单位，不能换算", field=field, expression=text)
        if qtype.dims != target.dims:
            raise _err(
                code,
                f"单位不兼容：推导量纲 {qtype.dimension_text}（{qtype.symbol}）"
                f" 与目标 {target.symbol}（{dimension_label(target.dims, target.currency)}）",
                field=field, expression=text,
                details={"from": qtype.to_dict(), "to": target.to_dict()},
            )
        if temperature_family(qtype.kind) or temperature_family(target.kind):
            if temperature_family(qtype.kind) != temperature_family(target.kind):
                raise _err(code,
                           f"温标类型不同：{qtype.label} 不能换算为 {target.label}",
                           field=field, expression=text)
        if qtype.kind == "money" and target.currency and qtype.currency and \
                target.currency != qtype.currency:
            if linear_factor(qtype.as_unit(), target, self.fx) is None:
                raise _err(
                    "CURRENCY_RATE_MISSING",
                    f"缺少 {qtype.currency}→{target.currency} 汇率",
                    field=field, expression=text,
                )
        if (qtype.kind == "money") != (target.kind == "money"):
            # a price (money/energy) keeps kind money in our model
            if qtype.kind == "money" and target.currency is None:
                raise _err(code, f"{qtype.symbol} 是货币量，目标 {target.symbol} 不是",
                           field=field, expression=text)
        if qtype.kind == "ratio" and target.kind not in ("ratio", "number"):
            if not (qtype.currency is None and target.kind == "quantity"):
                raise _err(code,
                           f"比例 {qtype.symbol} 不能直接换算为 {target.symbol}",
                           field=field, expression=text)
        if qtype.kind == "count" and target.kind != "count":
            raise _err(code, f"数量 {qtype.symbol} 不能换算为 {target.symbol}",
                       field=field, expression=text)


def analyze(ruleset, fx=None):
    return Analyzer(ruleset, fx).run()
