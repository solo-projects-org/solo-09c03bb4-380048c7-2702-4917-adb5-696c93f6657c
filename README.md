# 配送数仓列依赖分析服务

运单源表即将停供旧邮编列。本服务接收源表定义、各模型的 DuckDB SELECT 与待下线源列，
**只用 SQLGlot 解析、不执行任何 SQL**，沿别名、普通星号与非递归 CTE 跨模型追踪列引用，
按模型输出：

- 依赖该源列的**输出列**，每项附完整引用链；
- 依赖该源列的 **WHERE / JOIN ON（含 USING）/ QUALIFY 条件**，每项附引用链。
  条件依赖**不计入**输出列。

## 追踪规则

- 别名：`receiver_zip_old AS zip` 会把依赖从 `zip` 继续追到源列。
- 普通星号：`SELECT *`、`SELECT t.*` 按关系实际输出列展开；支持
  `* EXCLUDE (...)`（DuckDB 里 `EXCEPT` 同义）与 `* REPLACE (...)`。
- 非递归 CTE：CTE 可引用同模型内更早的 CTE，作用域链以 `模型.CTE.列` 呈现。
- 跨模型：模型可以引用其他模型的输出列，分析按依赖拓扑顺序进行，链上保留模型跳。
- 名称匹配大小写不敏感，源表可带 catalog/schema 前缀（按表名匹配）。
- 引用链自当前模型的输出列/条件列起，到 `源表.源列` 止。

## 含点名称与作用域（DuckDB 引号规则）

模型名、CTE 名和列名里都可能含点。分析器按 SQL 的词法区分两种点：

- **引号内的点**是名称本身的一部分，例如 `"carrier.postcode"` 是一个名为
  `carrier.postcode` 的模型/CTE/列。
- **引号外的点**是路径分隔符，例如 CTE 名 `carrier.postcode` 指模型 `carrier`
  里的 CTE `postcode`；`t."zip.old"` 是别名 `t` 的列 `zip.old`。
  注意源表/模型引用按 `Table` 的名字解析（`carrier.postcode` 在 FROM 中即
  schema `carrier` 的表 `postcode`），要引用名为 `carrier.postcode` 的源表或模型
  必须写成 `"carrier.postcode"`。

作用域内部以“标识符段元组”记录（`模型` → `CTE` → …），所以模型
`carrier.postcode` 与模型 `carrier` 的 CTE `postcode` 是两个独立作用域，
输出列和条件不会互相串用。

输出时按 DuckDB 双引号规则渲染（含点的段加双引号，名称内的双引号写成 `""`）：

| 内部路径 | 渲染结果 |
| --- | --- |
| 模型 `carrier` 的 CTE `postcode` | `carrier.postcode` |
| 模型 `carrier.postcode` | `"carrier.postcode"` |
| 列 `zip.old` | `"zip.old"` |
| 列 `a"b` | `"a""b"` |

普通名称的渲染与响应结构完全不变（不会凭空加引号）。

跨模型或嵌套 CTE 时，每条链都追到实际引用的源列；经不同模型或不同 CTE
到达同一源列的链全部保留（例如 `COALESCE(c1.zip, c2.zip)` 输出两条链）。
同名条件在不同作用域分别报告，`scope` 字段即该作用域路径。

## 明确不支持（提交即 400 并指出所在模型）

- 聚合函数、`GROUP BY`、`HAVING`；
- 集合运算 `UNION / INTERSECT / EXCEPT`；
- CTE 之外的子查询（FROM 子查询、标量子查询、`EXISTS` 等）；
- 递归 CTE（`WITH RECURSIVE`）、`LATERAL`、`UNNEST`/表函数、`NATURAL JOIN`。

## 找不到或有歧义的引用（400，指出模型）

- 列在当前查询所有关系中都不存在；
- 未限定的列名可由多个关系提供（歧义，要求加表别名）；
- 表别名不存在、表/CTE 未定义、输出列重名等。

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务: http://localhost:8000
# Swagger: http://localhost:8000/docs
# OpenAPI: http://localhost:8000/openapi.json
```

Swagger 的 `POST /analyze` 请求体自带两个样例，可直接 Try it out：

1. **模型名与 CTE 路径含点相撞**——模型 `carrier.postcode` 与模型 `carrier`
   内的 CTE `postcode` 同名含点，输出列与条件分别追踪、按引号规则显示；
2. **邮编改名**——使用普通名称的跨模型/CTE 样例。

## 请求示例

`examples/` 下有两个完整样例（也可用 Swagger 内置样例）：

- `examples/request_dotted_scopes.json`：模型 `carrier.postcode` 与 CTE
  `carrier.postcode` 相撞的运单场景；
- `examples/request_zip_rename.json`：普通名称的邮编下线场景。

```bash
curl -X POST http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/request_dotted_scopes.json
```

## 含点名称示例

```json
{
  "sources": [{"name": "waybills", "columns": ["waybill_id", "receiver_zip_old"]}],
  "models": [
    {"name": "carrier.postcode",
     "sql": "SELECT receiver_zip_old AS postcode_zip FROM waybills"},
    {"name": "carrier",
     "sql": "WITH postcode AS (SELECT receiver_zip_old AS zip FROM waybills WHERE receiver_zip_old IS NOT NULL) SELECT zip FROM postcode"},
    {"name": "dwd_delivery_fact",
     "sql": "SELECT c.zip AS carrier_zip, p.postcode_zip FROM carrier c JOIN \"carrier.postcode\" p ON c.waybill_id = p.waybill_id"}
  ],
  "targets": [{"table": "waybills", "column": "receiver_zip_old"}]
}
```

响应中模型 `carrier.postcode` 的链以 `"carrier.postcode".postcode_zip` 起，
模型 `carrier` 的链经过 `carrier.postcode.zip`（其 CTE 作用域），两者不混用。

请求结构：

```json
{
  "sources": [
    {"name": "waybills", "columns": ["waybill_id", "receiver_zip_old", "city_id"]}
  ],
  "models": [
    {"name": "stg_waybill", "sql": "WITH cleaned AS (SELECT waybill_id, receiver_zip_old AS zip FROM waybills WHERE receiver_zip_old IS NOT NULL) SELECT * FROM cleaned"}
  ],
  "targets": [
    {"table": "waybills", "column": "receiver_zip_old"}
  ]
}
```

响应结构（按模型分组）：

```json
{
  "targets": [{"table": "waybills", "column": "receiver_zip_old"}],
  "models": {
    "stg_waybill": {
      "dependent_output_columns": [
        {
          "output_column": "zip",
          "reference_chains": [
            ["stg_waybill.zip", "stg_waybill.cleaned.zip", "waybills.receiver_zip_old"]
          ]
        }
      ],
      "filter_conditions": [
        {
          "type": "WHERE",
          "scope": "stg_waybill.cleaned",
          "expression": "receiver_zip_old IS NOT NULL",
          "column": "receiver_zip_old",
          "reference_chain": ["stg_waybill.cleaned.zip", "waybills.receiver_zip_old"]
        }
      ]
    }
  }
}
```

错误响应统一为 `400`，`detail` 是数组，每项含 `model`、`location`、`message`，
一次返回收集到的全部问题。

## 本地开发（不用 Docker）

```bash
python -m venv --without-pip .venv   # 若系统带 ensurepip 可直接 python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest                       # 51 个测试
```

## 目录

```
app/
  main.py        FastAPI 路由、请求/响应模型、Swagger 样例与入参校验
  lineage.py     SQLGlot 解析与列级血缘分析（不执行 SQL，按 DuckDB 引号规则渲染名称）
  tests/         分析器与 API 测试
examples/        含点相撞、邮编改名两个完整请求样例
Dockerfile, docker-compose.yml, requirements.txt
```
