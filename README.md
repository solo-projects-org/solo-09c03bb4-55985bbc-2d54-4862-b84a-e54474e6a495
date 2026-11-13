# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**；分卷可能比总目先采到，服务同时提供**分卷归属查询**，按卷册 OAI 标识逐级求解所属在架总目，或给出需补采、请源馆更正的上级与原因。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` |
| 源馆在 dc:relation 多值中至多一项 `isPartOf: <OAI 标识>` 声明上级 | 只取首个 `isPartOf:` 前缀项（大小写不敏感），其余 relation 值忽略；未声明上级的在架书目视为总目 |
| 逐级归属到在架总目返回总目标识 | `GET /api/v1/attribution/{identifier}` 返回 `root_identifier` 与完整上溯链 |
| 上级缺失、撤下、引用成环返回原因和相关标识 | 状态 `parent_missing` / `parent_withdrawn` / `cycle`，附 `reason`、`parent_identifier`、`chain` |
| 归属存入 SQLite，升级时用旧目录补建，不改续采进度 | 新表 `attributions`；`init_db` 仅对缺失行补建，`harvest_progress` 不触碰 |
| 补采只重算本批书目及受影响下级卷册，不重扫全库 | 本批标识 + 沿 `parent_identifier` 向下闭包反查的下级，在采集事务内重算 |
| 归属与目录、截止日一起提交，失败都不变 | 归属重算位于采集事务内，异常整体回滚，截止日与归属均保持旧值 |
| 未决归属清单（待补采/待更正） | `GET /api/v1/attributions/unresolved` |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 发起采集并查询目录、进度与归属 | http://localhost:8000/docs |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/目录/归属/进度/运行历史/健康检查）
  harvester.py        采集编排：翻页、令牌失效重抓、整批事务、归属增量重算
  attribution.py      isPartOf 解析与逐级归属求解（缺失/撤下/成环）
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构、事务操作、归属读写与旧库补建
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           31 条样例记录（rec-013 删除），含总目/分卷层级与三类未决样例
  Dockerfile
tests/                pytest 端到端测试（28 个用例）
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1,
  "attributions_recomputed": 15
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 分卷归属查询：先采分卷，再补采上级

样例源中 `rec-005`/`rec-006` 是总目 `rec-028`（datestamp 10-28）的分卷。
首轮只采到 10-15 时，分卷已在架而总目尚未采到，归属为**上级缺失**：

```bash
# 首轮：分卷先到
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'

# 查分卷归属：parent_missing，给出缺失上级标识与原因
curl http://localhost:8000/api/v1/attribution/oai:sample.lib:rec-005
# {
#   "identifier": "oai:sample.lib:rec-005",
#   "record_status": "active",
#   "status": "parent_missing",
#   "root_identifier": null,
#   "parent_identifier": "oai:sample.lib:rec-028",
#   "reason": "上级 oai:sample.lib:rec-028 本地缺失，需补采该总目或请源馆更正",
#   "chain": ["oai:sample.lib:rec-005", "oai:sample.lib:rec-028"]
# }

# 编目员看待处理清单（缺失/撤下/成环）
curl http://localhost:8000/api/v1/attributions/unresolved

# 补采上级（不传 from，自动从上次截止日续采）
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-29"}'

# 再查：已归属到在架总目；本批只重算本批书目和受影响下级，不重扫全库
curl http://localhost:8000/api/v1/attribution/oai:sample.lib:rec-005
# {"status": "resolved", "root_identifier": "oai:sample.lib:rec-028", ...}
```

归属状态含义：

| status | 含义 | 编目动作 |
| --- | --- | --- |
| `root` | 在架且未声明上级，自身即总目 | 无 |
| `resolved` | 逐级上溯到在架总目 | 无，使用 `root_identifier` |
| `parent_missing` | 上级标识本地目录不存在 | 按 `parent_identifier` 补采，或请源馆更正 |
| `parent_withdrawn` | 上级已撤下（含删除墓碑） | 请源馆更正 isPartOf 声明 |
| `cycle` | 上溯链引用成环 | 按 `chain`/`reason` 请源馆更正 |

样例源内置的未决演示数据：`rec-009 -> rec-030`（源中不存在，缺失）、
`rec-010 -> rec-013`（rec-013 已删除，撤下）、`rec-011 <-> rec-012`（成环）；
多级链 `rec-008 -> rec-007 -> rec-029` 可验证逐级上溯。

### 5. 无匹配也推进截止日

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进。

## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 31 条共 4 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-29"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更回滚、
截止日保持不变，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、**归属增量重算**、截止日推进、运行历史更新；
- 归属重算范围 = 本批书目 ∪ 以本批记录为上级的下级卷册（沿 `attributions.parent_identifier` 向下闭包），**不重扫全库**；
- 事务中任何一步失败都会 rollback，书目、归属、截止日全部回到采集前；
- 服务升级时 `init_db` 建新表并用旧目录补建缺失归属，独立事务、只增不覆盖，`harvest_progress` 续采进度不变；
- 进程内互斥锁保证同一时刻只有一个采集任务，并发请求返回 `409`。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
