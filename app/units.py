"""Unit algebra for the analysis warehouse.

The model is deliberately explicit and conservative:

* physical quantities carry a dimension vector over
  ``length, mass, time, current, temperature, amount, luminosity, count, data``;
* every unit also has a *kind* (``ratio``, ``count``, ``money``,
  ``temperature``, ...) so dimensionless values such as percentages and item
  counts never silently mix;
* temperatures are affine units.  ``°C``/``°F`` carry an additive offset and
  may not be fed into a plain multiplication; convert with ``TO_KELVIN`` (or
  use ``TEMP_DIFF``) first;
* currencies are tagged.  Adding CNY and USD requires a configured FX rate,
  otherwise the rule is rejected.

All values inside the engine are canonical: SI base units for physical
quantities, kelvin for temperatures, the unit's own amount for money and a
plain fraction for ratios.
"""

from __future__ import annotations

from fractions import Fraction

from .errors import EngineError

DIM_NAMES = (
    "length", "mass", "time", "current", "temperature",
    "amount", "luminosity", "count", "data",
)
DIM_LABELS = {
    "length": "长度", "mass": "质量", "time": "时间", "current": "电流",
    "temperature": "温度", "amount": "物质的量", "luminosity": "发光强度",
    "count": "数量", "data": "数据量",
}
BASE_SYMBOLS = {
    "length": "m", "mass": "kg", "time": "s", "current": "A",
    "temperature": "K", "amount": "mol", "luminosity": "cd",
    "count": "count", "data": "byte",
}
ZERO_DIMS = tuple(Fraction(0) for _ in DIM_NAMES)


def dim(**kwargs) -> tuple[Fraction, ...]:
    vals = [Fraction(0)] * len(DIM_NAMES)
    for name, power in kwargs.items():
        vals[DIM_NAMES.index(name)] = Fraction(power)
    return tuple(vals)


def F(value) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


def _combine_dims(a, b, sign):
    return tuple(x + sign * y for x, y in zip(a, b))


class UnitError(EngineError):
    pass


class Unit:
    """A concrete unit.

    ``canonical = raw * scale + offset``.  ``scale`` is exact when possible.
    """

    __slots__ = ("dims", "scale", "offset", "kind", "symbol", "currency",
                 "category", "label")

    def __init__(self, dims, scale=1, offset=0, kind="quantity",
                 symbol="", currency=None, category="", label=""):
        self.dims = tuple(Fraction(x) for x in dims)
        self.scale = F(scale)
        self.offset = F(offset)
        self.kind = kind
        self.symbol = symbol
        self.currency = currency
        self.category = category
        self.label = label or symbol

    # -- basics ---------------------------------------------------------
    @property
    def is_affine(self):
        return self.offset != 0

    @property
    def dimensionless(self):
        return all(x == 0 for x in self.dims) and self.currency is None

    def to_canonical(self, value):
        return float(value) * float(self.scale) + float(self.offset)

    def from_canonical(self, value):
        return (float(value) - float(self.offset)) / float(self.scale)

    def canonical(self, symbol=None):
        """The canonical (scale 1, offset 0) unit with the same dimension."""
        return Unit(self.dims, 1, 0, self.kind,
                    symbol or canonical_symbol(self.dims, self.kind, self.currency),
                    self.currency)

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "label": self.label,
            "kind": self.kind,
            "category": self.category,
            "currency": self.currency,
            "dimension": dimension_label(self.dims, self.currency),
            "is_affine": self.is_affine,
            "scale": float(self.scale),
            "offset": float(self.offset),
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Unit {self.symbol} kind={self.kind} dims={self.dims}>"


def unit(symbol, dims, scale=1, offset=0, kind="quantity", currency=None,
         category="", label=""):
    return Unit(dims, scale, offset, kind, symbol, currency, category, label)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

_U = {}


def _reg(u):
    _U[u.symbol] = u
    return u


D_LEN = dim(length=1)
D_MASS = dim(mass=1)
D_TIME = dim(time=1)
D_TEMP = dim(temperature=1)
D_COUNT = dim(count=1)
D_DATA = dim(data=1)
D_ENERGY = dim(length=2, mass=1, time=-2)
D_POWER = dim(length=2, mass=1, time=-3)
D_SPEED = dim(length=1, time=-1)
D_VOLUME = dim(length=3)
D_AREA = dim(length=2)
D_PRESSURE = dim(length=-1, mass=1, time=-2)
D_FREQ = dim(time=-1)
D_CHARGE = dim(time=1, current=1)
D_VOLT = dim(length=2, mass=1, time=-3, current=-1)
D_RESIST = dim(length=2, mass=1, time=-3, current=-2)

# length
for sym, sc, label in [("m", 1, "米"), ("km", 1000, "千米"), ("cm", "0.01", "厘米"),
                       ("mm", "0.001", "毫米"), ("mi", "1609.344", "英里"),
                       ("ft", "0.3048", "英尺")]:
    _reg(unit(sym, D_LEN, sc, category="length", label=label))
# mass
for sym, sc, label in [("kg", 1, "千克"), ("g", "0.001", "克"),
                       ("t", 1000, "吨"), ("lb", "0.45359237", "磅")]:
    _reg(unit(sym, D_MASS, sc, category="mass", label=label))
# time
for sym, sc, label in [("s", 1, "秒"), ("min", 60, "分钟"), ("h", 3600, "小时"),
                       ("day", 86400, "天")]:
    _reg(unit(sym, D_TIME, sc, category="time", label=label))
# current
_reg(unit("A", dim(current=1), category="current", label="安培"))
# temperature
_reg(unit("K", D_TEMP, 1, 0, "temperature_abs", category="temperature", label="开尔文"))
_reg(unit("°C", D_TEMP, 1, "273.15", "temperature", category="temperature", label="摄氏度"))
_reg(unit("°F", D_TEMP, Fraction(5, 9), Fraction(45967, 180), "temperature",
          category="temperature", label="华氏度"))
_reg(unit("Δ°C", D_TEMP, 1, 0, "temperature_delta", category="temperature",
          label="摄氏度温差"))
_reg(unit("Δ°F", D_TEMP, Fraction(5, 9), 0, "temperature_delta",
          category="temperature", label="华氏度温差"))
_reg(unit("ΔK", D_TEMP, 1, 0, "temperature_delta", category="temperature",
          label="开尔文温差"))
# amount / luminosity
_reg(unit("mol", dim(amount=1), category="amount", label="摩尔"))
_reg(unit("cd", dim(luminosity=1), category="luminosity", label="坎德拉"))
# count
for sym, sc, label in [("count", 1, "件"), ("kcount", 1000, "千件"),
                       ("Mcount", 1000000, "百万件")]:
    _reg(unit(sym, D_COUNT, sc, kind="count", category="count", label=label))
# data
for sym, sc, label in [("byte", 1, "字节"), ("kB", 1000, "千字节"),
                       ("MB", 1000000, "兆字节"), ("GB", 1000000000, "吉字节")]:
    _reg(unit(sym, D_DATA, sc, category="data", label=label))
# ratio
_reg(unit("1", ZERO_DIMS, 1, kind="number", category="ratio", label="标量"))
_reg(unit("ratio", ZERO_DIMS, 1, kind="ratio", category="ratio", label="比例"))
_reg(unit("%", ZERO_DIMS, "0.01", kind="ratio", category="ratio", label="百分比"))
_reg(unit("ppm", ZERO_DIMS, "0.000001", kind="ratio", category="ratio", label="百万分比"))
# money
for sym, label in [("CNY", "人民币"), ("USD", "美元"), ("EUR", "欧元"),
                   ("JPY", "日元"), ("GBP", "英镑")]:
    _reg(unit(sym, ZERO_DIMS, 1, kind="money", currency=sym,
              category="money", label=label))
# energy / power
for sym, sc, label in [("J", 1, "焦耳"), ("kJ", 1000, "千焦"),
                       ("MJ", 1000000, "兆焦"), ("Wh", 3600, "瓦时"),
                       ("kWh", 3600000, "千瓦时"), ("MWh", 3600000000, "兆瓦时")]:
    _reg(unit(sym, D_ENERGY, sc, category="energy", label=label))
for sym, sc, label in [("W", 1, "瓦"), ("kW", 1000, "千瓦"), ("MW", 1000000, "兆瓦"),
                       ("hp", "745.699872", "马力")]:
    _reg(unit(sym, D_POWER, sc, category="power", label=label))
# volume / area
for sym, sc, label in [("m³", 1, "立方米"), ("L", "0.001", "升"),
                       ("mL", "0.000001", "毫升"), ("gal", "0.003785411784", "美制加仑")]:
    _reg(unit(sym, D_VOLUME, sc, category="volume", label=label))
for sym, sc, label in [("m²", 1, "平方米"), ("km²", 1000000, "平方千米"),
                       ("ha", 10000, "公顷")]:
    _reg(unit(sym, D_AREA, sc, category="area", label=label))
# speed
for sym, sc, label in [("m/s", 1, "米每秒"), ("km/h", Fraction(5, 18), "千米每小时"),
                       ("mph", "0.44704", "英里每小时")]:
    _reg(unit(sym, D_SPEED, sc, category="speed", label=label))
# intensity units (energy per distance)
for sym, sc, label in [("kWh/km", 3600, "千瓦时每千米"),
                       ("kWh/100km", 36, "千瓦时每百千米"),
                       ("Wh/km", 3.6, "瓦时每千米")]:
    _reg(unit(sym, dim(length=1, mass=1, time=-2), sc,
              category="intensity", label=label))
for sym, sc, label in [("L/100km", Fraction(1, 100000000), "升每百千米"),
                       ("L/km", Fraction(1, 1000000), "升每千米")]:
    _reg(unit(sym, dim(length=2), sc, category="intensity", label=label))
# pressure / frequency
for sym, sc, label in [("Pa", 1, "帕斯卡"), ("kPa", 1000, "千帕"),
                       ("MPa", 1000000, "兆帕"), ("bar", 100000, "巴")]:
    _reg(unit(sym, D_PRESSURE, sc, category="pressure", label=label))
for sym, sc, label in [("Hz", 1, "赫兹"), ("kHz", 1000, "千赫兹")]:
    _reg(unit(sym, D_FREQ, sc, category="frequency", label=label))
# electrical
for sym, dims, label in [("C", D_CHARGE, "库仑"), ("V", D_VOLT, "伏特"),
                         ("Ω", D_RESIST, "欧姆")]:
    _reg(unit(sym, dims, category="electrical", label=label))
# money per measure.  The dimension is the *inverse* of the measured
# dimension, so ``quantity [m³] × price [CNY/m³] = amount [CNY]`` cancels
# cleanly.  Canonical prices are currency per SI base unit.
D_VOLUME_INV = dim(length=-3)
D_ENERGY_INV = dim(length=-2, mass=-1, time=2)
D_MASS_INV = dim(mass=-1)
D_COUNT_INV = dim(count=-1)
D_LEN_INV = dim(length=-1)
D_TIME_INV = dim(time=-1)
for sym, dims, sc, cur, label in [
    ("CNY/kWh", D_ENERGY_INV, Fraction(1, 3600000), "CNY", "元每千瓦时"),
    ("CNY/MWh", D_ENERGY_INV, Fraction(1, 3600000000), "CNY", "元每兆瓦时"),
    ("CNY/J", D_ENERGY_INV, 1, "CNY", "元每焦耳"),
    ("USD/kWh", D_ENERGY_INV, Fraction(1, 3600000), "USD", "美元每千瓦时"),
    ("CNY/L", D_VOLUME_INV, 1000, "CNY", "元每升"),
    ("CNY/m³", D_VOLUME_INV, 1, "CNY", "元每立方米"),
    ("CNY/count", D_COUNT_INV, 1, "CNY", "元每件"),
    ("CNY/kg", D_MASS_INV, 1, "CNY", "元每千克"),
    ("CNY/t", D_MASS_INV, Fraction(1, 1000), "CNY", "元每吨"),
    ("CNY/km", D_LEN_INV, Fraction(1, 1000), "CNY", "元每千米"),
    ("CNY/h", D_TIME_INV, Fraction(1, 3600), "CNY", "元每小时"),
    ("USD/count", D_COUNT_INV, 1, "USD", "美元每件"),
]:
    _reg(unit(sym, dims, sc, kind="money", currency=cur, category="price", label=label))

UNITS = _U

CATEGORY_LABELS = {
    "length": "长度", "mass": "质量", "time": "时间", "current": "电流",
    "temperature": "温度", "amount": "物质的量", "luminosity": "发光强度",
    "count": "数量", "data": "数据量", "ratio": "比例", "money": "货币",
    "energy": "能量", "power": "功率", "volume": "体积", "area": "面积",
    "speed": "速度", "pressure": "压力", "frequency": "频率",
    "electrical": "电学", "price": "单价", "intensity": "单位消耗",
}


def get_unit(symbol):
    try:
        return UNITS[symbol]
    except KeyError:
        raise UnitError("UNKNOWN_UNIT_SYMBOL", f"未登记的单位 {symbol!r}",
                        details={"symbol": symbol})


def unit_catalog():
    out = []
    for u in UNITS.values():
        d = u.to_dict()
        d["category_label"] = CATEGORY_LABELS.get(u.category, u.category)
        out.append(d)
    return sorted(out, key=lambda x: (x["category"], x["label"]))


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

KNOWN_DERIVED = [
    (D_SPEED, "m/s"), (dim(length=1, time=-2), "m/s²"),
    (dim(length=1, mass=1, time=-2), "N"),
    (D_ENERGY, "J"), (D_POWER, "W"), (D_PRESSURE, "Pa"), (D_FREQ, "Hz"),
    (D_AREA, "m²"), (D_VOLUME, "m³"), (D_CHARGE, "C"), (D_VOLT, "V"),
    (D_RESIST, "Ω"),
]


def _power_symbol(base, power):
    if power == 1:
        return base
    if power.denominator == 1:
        return f"{base}^{power.numerator}"
    return f"{base}^{power.numerator}/{power.denominator}"


def canonical_symbol(dims, kind, currency=None):
    if kind == "ratio":
        return "ratio"
    if kind == "temperature_delta":
        return "ΔK"
    if kind in ("temperature", "temperature_abs"):
        return "K"
    if kind == "money" and all(x == 0 for x in dims):
        return currency or "money"
    if kind == "number":
        return "1"
    if kind == "count" and dims == D_COUNT:
        return "count"
    if kind == "ratio":
        return "ratio"
    for known_dims, sym in KNOWN_DERIVED:
        if dims == known_dims and not currency:
            return sym
    num, den = [], []
    if currency:
        num.append(currency)
    for name, power in zip(DIM_NAMES, dims):
        if power > 0:
            num.append(_power_symbol(BASE_SYMBOLS[name], power))
        elif power < 0:
            den.append(_power_symbol(BASE_SYMBOLS[name], -power))
    if not num and not den:
        return "1"
    top = "·".join(num) if num else "1"
    if not den:
        return top
    bottom = "·".join(den)
    if len(den) == 1 and all(x == 0 for x in dims) is False:
        return f"{top}/{bottom}"
    return f"{top}/({bottom})"


def dimension_label(dims, currency=None):
    parts = []
    if currency:
        parts.append(currency)
    for name, power in zip(DIM_NAMES, dims):
        if power:
            s = DIM_LABELS[name]
            if power != 1:
                s += f"^{power.numerator}" if power.denominator == 1 else \
                    f"^({power.numerator}/{power.denominator})"
            parts.append(s)
    return "·".join(parts) if parts else "无量纲"


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

def fx_rate(fx, src, dst):
    """Return units of ``dst`` per one ``src``, or ``None`` when unknown."""
    if src == dst:
        return 1.0
    if not fx:
        return None
    direct = fx.get(f"{src}>{dst}")
    if direct is not None:
        return float(direct)
    inverse = fx.get(f"{dst}>{src}")
    if inverse:
        return 1.0 / float(inverse)
    return None


def same_dimension(a, b):
    return a.dims == b.dims


def temperature_family(kind):
    if kind in ("temperature", "temperature_abs"):
        return "absolute"
    if kind == "temperature_delta":
        return "delta"
    return None


def linear_factor(src, dst, fx=None):
    """Factor converting a linear value of ``src`` into ``dst``.

    ``None`` means incompatible.  Affine offsets are not applied here.
    """
    if src.dims != dst.dims:
        return None
    sf, df = temperature_family(src.kind), temperature_family(dst.kind)
    if sf or df:
        if sf != df:
            return None
    if (src.kind == "money") != (dst.kind == "money"):
        return None
    factor = float(src.scale) / float(dst.scale)
    if src.currency and dst.currency and src.currency != dst.currency:
        rate = fx_rate(fx, src.currency, dst.currency)
        if rate is None:
            return None
        factor *= rate
    elif bool(src.currency) != bool(dst.currency):
        return None
    return factor


def convert(value, src, dst, fx=None, label=None):
    """Convert a value and return ``(value, steps)``."""
    if src.dims != dst.dims:
        raise UnitError("UNIT_MISMATCH",
                        f"{label or ''}单位 {src.symbol} 与 {dst.symbol} 量纲不同",
                        details={"from": src.symbol, "to": dst.symbol})
    sf, df = temperature_family(src.kind), temperature_family(dst.kind)
    if sf or df:
        if sf != df:
            raise UnitError("TEMPERATURE_KIND_MISMATCH",
                            f"绝对温度 {src.symbol} 不能直接与温差 {dst.symbol} 互换")
    steps = []
    rate = None
    if src.currency and dst.currency and src.currency != dst.currency:
        rate = fx_rate(fx, src.currency, dst.currency)
        if rate is None:
            raise UnitError(
                "CURRENCY_RATE_MISSING",
                f"缺少 {src.currency}→{dst.currency} 汇率，不能换算",
                details={"from": src.currency, "to": dst.currency},
            )
    if not src.is_affine and not dst.is_affine:
        # Pure scaling: one honest step, no detour through base units.
        factor = src.scale / dst.scale
        if rate is not None:
            factor *= F(rate)
        result = float(value) * float(factor)
        if float(factor) != 1.0:
            extra = {"rate": rate} if rate is not None else {}
            steps.append({
                "from": value, "from_unit": src.symbol,
                "to": result, "to_unit": dst.symbol,
                "factor": float(factor), "offset": 0.0, **extra,
                "formula": f"{_fmt(value)} {src.symbol} × {_fmt(float(factor))}"
                           f" = {_fmt(result)} {dst.symbol}",
                "value": result, "unit": dst.symbol,
            })
        return result, steps
    canonical = src.to_canonical(value)
    if src.is_affine or src.scale != 1:
        steps.append({
            "from": value, "from_unit": src.symbol,
            "to": canonical,
            "to_unit": canonical_symbol(src.dims, src.kind, src.currency),
            "factor": float(src.scale), "offset": float(src.offset),
            "formula": f"{_fmt(value)} {src.symbol} × {_fmt(float(src.scale))}"
                       + (f" + {_fmt(float(src.offset))}" if src.offset else "")
                       + f" = {_fmt(canonical)} {canonical_symbol(src.dims, src.kind, src.currency)}",
            "value": canonical,
            "unit": canonical_symbol(src.dims, src.kind, src.currency),
        })
    if rate is not None:
        before = canonical
        canonical = canonical * rate
        steps.append({
            "from": before, "from_unit": src.currency,
            "to": canonical, "to_unit": dst.currency,
            "rate": rate,
            "formula": f"{_fmt(before)} {src.currency} × {_fmt(rate)} = "
                       f"{_fmt(canonical)} {dst.currency}",
            "value": canonical,
            "unit": dst.currency,
        })
    result = (canonical - float(dst.offset)) / float(dst.scale)
    if dst.is_affine or dst.scale != 1:
        steps.append({
            "from": canonical, "from_unit": canonical_symbol(dst.dims, dst.kind, dst.currency),
            "to": result, "to_unit": dst.symbol,
            "factor": float(dst.scale), "offset": float(dst.offset),
            "formula": f"({_fmt(canonical)} − {_fmt(float(dst.offset))}) ÷ "
                       f"{_fmt(float(dst.scale))} = {_fmt(result)} {dst.symbol}",
            "value": result,
            "unit": dst.symbol,
        })
    return result, steps


def _fmt(x):
    if isinstance(x, Fraction):
        x = float(x)
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x))}"
    return f"{x:.10g}"


fmt = _fmt
