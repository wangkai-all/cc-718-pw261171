"""Sample dataset and default ruleset used by the demo warehouse."""

from __future__ import annotations

SAMPLE_NAME = "2026-11 充电站运营样本"

SAMPLE_COLUMNS = [
    "trip_id", "distance", "route_mode", "duration", "energy",
    "temperature_c", "temperature_k", "night_temp_c",
    "fee_cny", "fee_usd", "sessions", "streamed_bytes",
    "unit_price_cny", "discount_pct",
]

SAMPLE_ROWS = [
    ["T-1001", 120, "highway", 2.0, 24.0, 32, 305.15, 21.5, 68.0, 9.5, 8, 1536000, 0.65, 90],
    ["T-1002", 90, "city", 1.5, 15.5, 27, 300.15, 19.0, 54.0, 7.6, 6, 984000, 0.62, 85],
    ["T-1003", 45, "city", 1.25, 8.2, 18, 291.15, 12.5, 30.6, 4.3, 4, 512000, 0.60, 100],
    ["T-1004", 300, "highway", 3.5, 52.0, 35, 308.15, 24.0, 152.0, 21.5, 18, 3072000, 0.70, 75],
    ["T-1005", 72, "suburb", 1.8, 11.0, 22, 295.15, 15.0, 42.5, 6.0, 5, 720000, 0.55, 95],
    ["T-1006", 3.5, "city", 0.4, 1.2, 20, 293.15, 14.0, 12.0, 1.7, 1, 96000, 0.58, 100],
]

# ``temperature_c`` is deliberately an affine °C reading: subtracting two of
# them yields a temperature difference (Δ°C), never a temperature.
DEFAULT_FIELDS = [
    {"name": "distance", "kind": "raw", "unit": "km", "label": "行驶距离"},
    {"name": "duration", "kind": "raw", "unit": "h", "label": "耗时"},
    {"name": "energy", "kind": "raw", "unit": "kWh", "label": "充电电量"},
    {"name": "temperature_c", "kind": "raw", "unit": "°C", "label": "环境温度"},
    {"name": "temperature_k", "kind": "raw", "unit": "K", "label": "环境温度(绝对)"},
    {"name": "night_temp_c", "kind": "raw", "unit": "°C", "label": "夜间温度"},
    {"name": "fee_cny", "kind": "raw", "unit": "CNY", "label": "人民币费用"},
    {"name": "fee_usd", "kind": "raw", "unit": "USD", "label": "美元费用"},
    {"name": "sessions", "kind": "raw", "unit": "count", "label": "充电次数"},
    {"name": "streamed_bytes", "kind": "raw", "unit": "byte", "label": "回传数据量"},
    {"name": "unit_price_cny", "kind": "raw", "unit": "CNY/kWh", "label": "电价"},
    {"name": "discount_pct", "kind": "raw", "unit": "%", "label": "折扣"},
    {
        "name": "speed_kmh", "kind": "derived", "label": "平均速度",
        "expression": "distance / duration", "output_unit": "km/h",
    },
    {
        "name": "speed_ms", "kind": "derived", "label": "平均速度(米每秒)",
        "expression": "TO(speed_kmh, \"m/s\")", "output_unit": "m/s",
    },
    {
        "name": "temp_delta", "kind": "derived", "label": "昼夜温差",
        "expression": "temperature_c - night_temp_c", "output_unit": "Δ°C",
    },
    {
        "name": "energy_per_100km", "kind": "derived", "label": "百公里能耗",
        "expression": "energy / distance", "output_unit": "kWh/100km",
    },
    {
        "name": "cost_cny", "kind": "derived", "label": "充电成本",
        "expression": "energy * unit_price_cny", "output_unit": "CNY",
    },
    {
        "name": "total_fee_cny", "kind": "derived", "label": "费用合计(人民币)",
        "expression": "fee_cny + fee_usd", "output_unit": "CNY",
    },
    {
        "name": "cost_per_session", "kind": "derived", "label": "单次充电成本",
        "expression": "IF sessions > 0 THEN cost_cny / sessions ELSE 0 [CNY/count]",
        "output_unit": "CNY/count",
    },
    {
        "name": "discounted_cost", "kind": "derived", "label": "折后成本",
        "expression": "cost_cny * (1 - discount_pct)", "output_unit": "CNY",
    },
    {
        "name": "total_distance", "kind": "derived", "label": "总里程",
        "expression": "SUM(distance)", "output_unit": "km",
    },
    {
        "name": "avg_speed", "kind": "derived", "label": "加权平均速度",
        "expression": "SUM(distance) / SUM(duration)", "output_unit": "km/h",
    },
    {
        "name": "total_energy", "kind": "derived", "label": "总电量",
        "expression": "SUM(energy)", "output_unit": "kWh",
    },
]

DEFAULT_FX = {"USD>CNY": 7.2}
DEFAULT_OUTPUT = "speed_kmh"
