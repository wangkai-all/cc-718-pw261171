"""A small, deliberately non-Turing-complete expression language.

Supported syntax (no user code, no attribute access, no calls except the
whitelisted ``SUM`` / ``TO``):

    literals        12, 1.5e3, 30 [km/h], 20 %, true, false
    references      distance_km, speed
    arithmetic      + - * / ^ (integer powers)
    comparison      == != < <= > >=
    boolean         AND OR NOT
    conditional     IF condition THEN expr ELSE expr
    aggregate       SUM(expr)
    conversion      TO(expr, "km/h")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import EngineError
from .units import UNITS, EngineError as _BaseError  # noqa: F401  (re-export compat)
from .units import UnitError


@dataclass
class Node:
    pos: int = 0
    width: int = 0

    def refs(self):
        return set()

    def children(self):
        return []


@dataclass
class Literal(Node):
    value: float = 0.0
    unit: object = None      # Unit or None
    raw: str = ""

    def refs(self):
        return set()


@dataclass
class Boolean(Node):
    value: bool = False


@dataclass
class Ref(Node):
    name: str = ""

    def refs(self):
        return {self.name}


@dataclass
class Unary(Node):
    op: str = ""
    operand: Node = None

    def refs(self):
        return self.operand.refs()

    def children(self):
        return [self.operand]


@dataclass
class Binary(Node):
    op: str = ""
    left: Node = None
    right: Node = None

    def refs(self):
        return self.left.refs() | self.right.refs()

    def children(self):
        return [self.left, self.right]


@dataclass
class Compare(Node):
    op: str = ""
    left: Node = None
    right: Node = None

    def refs(self):
        return self.left.refs() | self.right.refs()

    def children(self):
        return [self.left, self.right]


@dataclass
class BoolOp(Node):
    op: str = ""
    values: list = field(default_factory=list)

    def refs(self):
        out = set()
        for v in self.values:
            out |= v.refs()
        return out

    def children(self):
        return list(self.values)


@dataclass
class If(Node):
    cond: Node = None
    then: Node = None
    otherwise: Node = None

    def refs(self):
        return self.cond.refs() | self.then.refs() | self.otherwise.refs()

    def children(self):
        return [self.cond, self.then, self.otherwise]


@dataclass
class Sum(Node):
    expr: Node = None

    def refs(self):
        return self.expr.refs()

    def children(self):
        return [self.expr]


@dataclass
class To(Node):
    expr: Node = None
    unit: object = None
    unit_symbol: str = ""

    def refs(self):
        return self.expr.refs()

    def children(self):
        return [self.expr]


@dataclass
class Token:
    kind: str
    value: object
    pos: int
    text: str


_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENT = re.compile(r"[A-Za-z_°一-鿿][A-Za-z0-9_°一-鿿]*")
_OPS = ("==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/", "^",
        "(", ")", ",", "%")
_KEYWORDS = {"IF", "THEN", "ELSE", "AND", "OR", "NOT", "SUM", "TO",
             "TRUE", "FALSE"}


class _Lexer:
    def __init__(self, text):
        self.text = text
        self.i = 0
        self.tokens = []

    def fail(self, message, pos=None):
        raise EngineError("EXPR_SYNTAX", message,
                          path=f"第 {(pos if pos is not None else self.i) + 1} 个字符")

    def lex(self):
        text = self.text
        n = len(text)
        while self.i < n:
            ch = text[self.i]
            if ch.isspace():
                self.i += 1
                continue
            start = self.i
            if ch == "[":
                end = text.find("]", start + 1)
                if end < 0:
                    self.fail("单位常量缺少右方括号 ]", start)
                symbol = text[start + 1:end].strip()
                if symbol not in UNITS:
                    raise EngineError(
                        "UNKNOWN_UNIT_SYMBOL",
                        f"未登记的单位 {symbol!r}；请先在单位表中登记",
                        path=f"第 {start + 1} 个字符",
                        details={"symbol": symbol},
                    )
                self.tokens.append(Token("UNIT", UNITS[symbol], start,
                                         text[start:end + 1]))
                self.i = end + 1
                continue
            if ch == '"':
                j = start + 1
                buf = []
                while j < n and text[j] != '"':
                    if text[j] == "\\" and j + 1 < n:
                        buf.append(text[j + 1])
                        j += 2
                    else:
                        buf.append(text[j])
                        j += 1
                if j >= n:
                    self.fail("字符串缺少右引号", start)
                self.tokens.append(Token("STRING", "".join(buf), start,
                                         text[start:j + 1]))
                self.i = j + 1
                continue
            m = _NUMBER.match(text, start)
            if m:
                self.tokens.append(Token("NUMBER", float(m.group(0)), start,
                                         m.group(0)))
                self.i = m.end()
                continue
            m = _IDENT.match(text, start)
            if m:
                word = m.group(0)
                upper = word.upper()
                if upper in _KEYWORDS:
                    self.tokens.append(Token(upper, upper, start, word))
                else:
                    self.tokens.append(Token("IDENT", word, start, word))
                self.i = m.end()
                continue
            for op in _OPS:
                if text.startswith(op, start):
                    self.tokens.append(Token(op, op, start, op))
                    self.i += len(op)
                    break
            else:
                self.fail(f"无法识别的字符 {ch!r}", start)
        self.tokens.append(Token("EOF", None, len(text), ""))
        return self.tokens


class _Parser:
    def __init__(self, text):
        self.text = text
        self.tokens = _Lexer(text).lex()
        self.i = 0

    @property
    def cur(self):
        return self.tokens[self.i]

    def advance(self):
        tok = self.cur
        self.i += 1
        return tok

    def fail(self, message, tok=None):
        tok = tok or self.cur
        raise EngineError("EXPR_SYNTAX", message,
                          path=f"第 {tok.pos + 1} 个字符",
                          details={"near": tok.text or "表达式结尾"})

    def expect(self, kind, message):
        if self.cur.kind != kind:
            self.fail(message)
        return self.advance()

    def parse(self):
        node = self.parse_or()
        if self.cur.kind != "EOF":
            self.fail(f"多余的内容 {self.cur.text!r}")
        return node

    def parse_or(self):
        first = self.parse_and()
        if self.cur.kind != "OR":
            return first
        values = [first]
        while self.cur.kind == "OR":
            self.advance()
            values.append(self.parse_and())
        return BoolOp(pos=first.pos, op="OR", values=values)

    def parse_and(self):
        first = self.parse_not()
        if self.cur.kind != "AND":
            return first
        values = [first]
        while self.cur.kind == "AND":
            self.advance()
            values.append(self.parse_not())
        return BoolOp(pos=first.pos, op="AND", values=values)

    def parse_not(self):
        if self.cur.kind == "NOT":
            tok = self.advance()
            return Unary(pos=tok.pos, op="NOT", operand=self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_additive()
        if self.cur.kind not in ("==", "!=", "<", "<=", ">", ">="):
            return left
        ops = []
        while self.cur.kind in ("==", "!=", "<", "<=", ">", ">="):
            tok = self.advance()
            right = self.parse_additive()
            ops.append((tok.kind, right))
        if len(ops) > 1:
            self.fail("不支持链式比较，请用 AND 连接两个比较")
        op, right = ops[0]
        return Compare(pos=left.pos, op=op, left=left, right=right)

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.cur.kind in ("+", "-"):
            tok = self.advance()
            right = self.parse_multiplicative()
            node = Binary(pos=node.pos, op=tok.kind, left=node, right=right)
        return node

    def parse_multiplicative(self):
        node = self.parse_unary()
        while self.cur.kind in ("*", "/"):
            tok = self.advance()
            right = self.parse_unary()
            node = Binary(pos=node.pos, op=tok.kind, left=node, right=right)
        return node

    def parse_unary(self):
        if self.cur.kind in ("+", "-"):
            tok = self.advance()
            return Unary(pos=tok.pos, op=tok.kind, operand=self.parse_unary())
        return self.parse_power()

    def parse_power(self):
        base = self.parse_primary()
        if self.cur.kind == "^":
            tok = self.advance()
            exponent = self.parse_unary()
            return Binary(pos=base.pos, op="^", left=base, right=exponent)
        return base

    def parse_primary(self):
        tok = self.cur
        if tok.kind == "NUMBER":
            self.advance()
            unit = None
            if self.cur.kind == "UNIT":
                unit = self.advance().value
            elif self.cur.kind == "%":
                self.advance()
                unit = UNITS["%"]
            return Literal(pos=tok.pos, value=float(tok.value), unit=unit,
                           raw=tok.text)
        if tok.kind == "UNIT":
            self.fail("单位常量前需要数值，例如 30 [km/h]")
        if tok.kind == "STRING":
            self.fail("字符串只能作为 TO 的第二个参数")
        if tok.kind in ("TRUE", "FALSE"):
            self.advance()
            return Boolean(pos=tok.pos, value=tok.kind == "TRUE")
        if tok.kind == "IF":
            self.advance()
            cond = self.parse_or()
            self.expect("THEN", "IF 条件后缺少 THEN")
            then = self.parse_or()
            self.expect("ELSE", "IF 分支后缺少 ELSE")
            otherwise = self.parse_or()
            return If(pos=tok.pos, cond=cond, then=then, otherwise=otherwise)
        if tok.kind == "SUM":
            self.advance()
            self.expect("(", "SUM 后需要左括号")
            expr = self.parse_or()
            self.expect(")", "SUM 缺少右括号")
            return Sum(pos=tok.pos, expr=expr)
        if tok.kind == "TO":
            self.advance()
            self.expect("(", "TO 后需要左括号")
            expr = self.parse_or()
            self.expect(",", "TO 需要两个参数：TO(表达式, \"单位\")")
            if self.cur.kind == "STRING":
                symbol = self.advance().value
            elif self.cur.kind == "UNIT":
                symbol = self.advance().value.symbol
            else:
                self.fail("TO 的第二个参数必须是单位字符串，例如 \"km/h\"")
            self.expect(")", "TO 缺少右括号")
            if symbol not in UNITS:
                raise EngineError(
                    "UNKNOWN_UNIT_SYMBOL", f"未登记的单位 {symbol!r}",
                    details={"symbol": symbol},
                )
            return To(pos=tok.pos, expr=expr, unit=UNITS[symbol],
                      unit_symbol=symbol)
        if tok.kind == "IDENT":
            self.advance()
            if self.cur.kind == "(":
                self.fail(f"不支持函数 {tok.value}()；仅支持 SUM 和 TO")
            return Ref(pos=tok.pos, name=tok.value)
        if tok.kind == "(":
            self.advance()
            node = self.parse_or()
            self.expect(")", "缺少右括号")
            return node
        self.fail(f"意外的符号 {tok.text!r}")

    # -- entry ----------------------------------------------------------
    def parse(self):  # noqa: F811
        node = self.parse_or()
        if self.cur.kind != "EOF":
            self.fail(f"多余的内容 {self.cur.text!r}")
        return node


def parse(text):
    if not text or not text.strip():
        raise EngineError("EMPTY_EXPRESSION", "表达式为空")
    return _Parser(text).parse()


def collect_refs(node):
    return node.refs()


def contains_sum(node):
    if isinstance(node, Sum):
        return True
    return any(contains_sum(c) for c in node.children())


def sum_nodes(node):
    out = []
    if isinstance(node, Sum):
        out.append(node)
    for child in node.children():
        out.extend(sum_nodes(child))
    return out


def has_aggregate_outside_sum(node, inside=False):
    """True when a reference or value is used outside any SUM()."""
    if isinstance(node, Ref) and not inside:
        return True
    if isinstance(node, Literal) and not inside:
        return False
    for child in node.children():
        if has_aggregate_outside_sum(child, inside or isinstance(node, Sum)):
            return True
    return False


def refs_outside_sum(node, inside=False):
    """References evaluated per-row (i.e. not inside an aggregate)."""
    out = set()
    if isinstance(node, Ref) and not inside:
        out.add(node.name)
    for child in node.children():
        out |= refs_outside_sum(child, inside or isinstance(node, Sum))
    return out


def const_number(node):
    """Fold a constant numeric subtree, or return None."""
    if isinstance(node, Literal):
        if node.unit is not None:
            return None
        return float(node.value)
    if isinstance(node, Unary) and node.op in ("+", "-"):
        v = const_number(node.operand)
        if v is None:
            return None
        return -v if node.op == "-" else v
    if isinstance(node, Binary):
        a = const_number(node.left)
        b = const_number(node.right)
        if a is None or b is None:
            return None
        try:
            if node.op == "+":
                return a + b
            if node.op == "-":
                return a - b
            if node.op == "*":
                return a * b
            if node.op == "/":
                if b == 0:
                    return None
                return a / b
            if node.op == "^":
                return a ** b
        except Exception:
            return None
    return None


def expression_text(node):
    """Render a normalized expression (used in annotations)."""
    if isinstance(node, Literal):
        unit = f" [{node.unit.symbol}]" if node.unit else ""
        number = node.raw if node.raw else f"{node.value:g}"
        return f"{number}{unit}"
    if isinstance(node, Boolean):
        return "true" if node.value else "false"
    if isinstance(node, Ref):
        return node.name
    if isinstance(node, Unary):
        if node.op == "NOT":
            return f"NOT {expression_text(node.operand)}"
        return f"{node.op}{expression_text(node.operand)}"
    if isinstance(node, Binary):
        return f"({expression_text(node.left)} {node.op} {expression_text(node.right)})"
    if isinstance(node, Compare):
        return f"({expression_text(node.left)} {node.op} {expression_text(node.right)})"
    if isinstance(node, BoolOp):
        return "(" + f" {node.op} ".join(expression_text(v) for v in node.values) + ")"
    if isinstance(node, If):
        return (f"IF {expression_text(node.cond)} THEN "
                f"{expression_text(node.then)} ELSE {expression_text(node.otherwise)}")
    if isinstance(node, Sum):
        return f"SUM({expression_text(node.expr)})"
    if isinstance(node, To):
        return f"TO({expression_text(node.expr)}, \"{node.unit_symbol}\")"
    return "?"
