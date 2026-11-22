# 量纲检查分析仓

为派生字段做**量纲（单位）检查**的分析仓演示：网页登记原始字段单位，用受控表达式定义速度、能耗、成本等新字段；发布前推导单位、展示每一步换算，并用真实样本数据加工出结果。

零第三方依赖：Python 标准库 + 原生 JS。

## 运行

```bash
python3 server.py --port 8765
# 打开 http://127.0.0.1:8765/
```

首次启动会在 `data/warehouse.db` 建库并写入「2026-11 充电站运营样本」数据集、初始草稿和 v1 版本。

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 页面能做什么

| 区域 | 功能 |
| --- | --- |
| 原始字段登记 | 为每个原始列选择单位；单位未知的字段不能参与派生计算 |
| 汇率输入 | USD→CNY 等汇率；缺失时人民币与美元不能相加，检查结果会指出具体字段 |
| 派生字段 | 受控表达式 + 输出单位 + 业务含义；卡片实时显示推导类型、量纲、依赖与下游 |
| 检查结果 | 每个错误带错误码、字段、表达式、受影响下游；有环则阻止发布 |
| 预览差异 | 不落库：规则差异（新增/删除/改动）+ 数据差异（哪些行、单位从→到、样本值） |
| 发布新版本 | 规则与输出数据一起绑定成不可变版本；失败只记录失败版本，继续读上一成功版本 |
| 加工结果 | 逐行输出、聚合值、点任意行查看完整换算链 |
| 版本记录 | 按版本读取历史输出（失败版本不可读） |

## 表达式语言

只为量纲分析设计，不开放任意代码：手写词法/语法分析，**没有 `eval`**，不允许属性访问和任意函数调用。

```
distance / duration                 # 速度，自动推导 km/h
TO(speed_kmh, "m/s")                # 显式单位换算
energy * unit_price_cny             # 用量 × 单价 = 金额
IF sessions > 0 THEN cost / sessions ELSE 0 [CNY/count]
SUM(distance) / SUM(duration)       # 聚合：加权平均速度
```

支持：`+ - * / ^`、比较 `> >= < <= == !=`、`IF/THEN/ELSE`、`AND/OR/NOT`、`SUM(...)`、`TO(expr, "unit")`、带单位常量 `30 [km/h]`、百分号 `90%`。

**SUM 与逐行字段不可混用**（`SUM(x) + x` 会报 `AGGREGATE_MIXED`）。

## 单位模型

- 单位 = **基本量纲向量 + 倍率（+ 温度偏移）**，全部用 `Fraction` 精确表示。
- 9 个基本量纲：长度、质量、时间、电流、温度、物质的量、发光强度、数量、数据量。
- 加减要求同量纲且可换算；乘除组合量纲。
- 维度相关的显式类型：物理量 / 标量 / 比例 / 数量 `count` / 货币 / 绝对温度 / 温度差 / 布尔。`count`、比例、货币都不是「无量纲」。
- 单价类单位（`CNY/kWh`、`CNY/km`、`CNY/count`）使用**逆量纲 + 每 SI 基本单位的币值**，所以 `kWh × CNY/kWh = CNY`、`CNY ÷ km = CNY/km` 都能精确相约。

### 温度

| 单位 | 类型 | 说明 |
| --- | --- | --- |
| `°C` `°F` | 绝对温度（带偏移） | 参与乘除前必须先 `TO(..., "K")`，否则报 `AFFINE_TEMPERATURE_MULTIPLY` |
| `K` | 绝对温标（无偏移） | 允许乘除 |
| `Δ°C` `Δ°F` `ΔK` | 温差 | 无偏移 |

- `°C − °C → Δ°C`（差值不是绝对温度）
- `°C + °C` → `AFFINE_TEMPERATURE_ADD`（两个读数相加无意义）
- 绝对温度 ± 温差 → 绝对温度；温差 − 绝对温度 → 拒绝

### 货币

- 同币种可直接加减。
- 不同币种必须提供汇率（如 `USD>CNY: 7.2`），否则报 `CURRENCY_RATE_MISSING` 并指向表达式与下游。
- 金额 × 金额、货币作分母、货币与数量相加都会被拒绝。

## 错误定位

每个诊断都带：`code`、`field`、`expression`（具体子表达式文本）、`affected_downstream`（受影响的下游字段）。上游字段出错时，下游会被标记为 `blocked`，不会把「未知」当成无量纲继续算。

常见错误码：`FIELD_UNIT_UNKNOWN`、`UNIT_MISMATCH`、`KIND_MISMATCH`、`BRANCH_UNIT_MISMATCH`、`DIVISION_BY_ZERO`、`DEPENDENCY_CYCLE`、`CURRENCY_RATE_MISSING`、`MONEY_PRODUCT`、`AFFINE_TEMPERATURE_MULTIPLY`、`AGGREGATE_MIXED`。

依赖图变化后会重新检查**全部**派生字段；检测到环会列出环上的字段并阻止发布。

## 版本与数据

- 草稿可反复编辑；`预览` 只在内存中计算候选规则，返回规则差异 + 数据差异，**不写库**。
- `发布` 把规则（字段定义 + 汇率）和输出（列、逐行值、聚合、换算步骤）绑定为同一版本。
- 运行期失败（如分母为零）同样记录为 `failed` 版本，**不会**替换最新成功版本；页面继续展示旧数据并在顶部提示。
- 读取接口一律取「最新成功版本」。

## 目录

```
app/units.py        单位表、精确量纲运算、换算与换算步骤
app/expressions.py  受控表达式词法/语法分析（无 eval）
app/analyzer.py     静态类型与量纲推导、依赖图、环检测、错误定位
app/runtime.py      在样本数据上求值，保留每一步的显示单位与换算链
app/store.py        SQLite：数据集、草稿、不可变版本、事件
app/sample.py       样本数据与默认字段定义
server.py           JSON API + 静态页面
web/                原生 JS 单页界面
tests/              引擎与存储测试
```

## API 速览

```
GET  /api/units
GET  /api/datasets                  POST /api/datasets            (CSV 导入)
GET  /api/datasets/{id}
GET  /api/datasets/{id}/draft       POST /api/datasets/{id}/draft
GET  /api/datasets/{id}/latest
GET  /api/datasets/{id}/versions
GET  /api/datasets/{id}/versions/{n}
POST /api/datasets/{id}/analyze
POST /api/datasets/{id}/preview
POST /api/datasets/{id}/publish
GET  /api/datasets/{id}/events
```

## 已验证的数值

| 场景 | 结果 |
| --- | --- |
| `distance / duration` | 120 km ÷ 2 h = 60 km/h |
| `TO(speed_kmh, "m/s")` | 60 km/h × 0.2777777778 = 16.66666667 m/s |
| 温度差 | 32 °C − 21.5 °C = 10.5 Δ°C |
| 绝对温度换算 | 20 °C = 68 °F = 293.15 K |
| 用量 × 单价 | 24 kWh × 0.65 CNY/kWh = 15.6 CNY |
| 汇率相加 | 9.5 USD × 7.2 + 68 CNY = 136.4 CNY |
| 百公里能耗 | 24 kWh ÷ 120 km = 0.2 kWh/km → 20 kWh/100km |
| 无效费用公式 | `fee_cny + sessions` → `UNIT_MISMATCH`，阻止发布 |
