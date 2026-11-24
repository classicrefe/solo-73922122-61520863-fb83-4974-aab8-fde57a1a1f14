# 验布作业卡供数台账

织厂数仓的验布供数网页应用：登记作业卡实检清单、提交批次（唯一批号、
所用卡、异常项目可空）、按项目查三种口径的批数，并在实检清单填错时更正
已入库批次的供数。Flask + SQLite，Docker Compose 启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000  （WEB_PORT=8080 docker compose up 可改端口）
```

数据存放在命名卷 `ledger-data`（容器内 `/data/inspection.sqlite3`）。
同一批卡与项目字典由应用启动时幂等写入。

本地不用 Docker 时：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
flask --app 'app:create_app()' run --debug
```

## 页面

- `/` 登记批次、更正各卡实检清单、已登记批次列表
- `/query?item_id=N` 单项目三口径批数：有异常 / 已检无异常 / 未检
- `/cards/<id>` 单卡清单与引用批次
- `/batches/<id>` 批次原异常明细（更正不改动）
- `/audit` 按原始批次逐条重算，与物化计数核对

## 口径与规则

- **唯一状态**：只有 `submitted` 一种已提交状态入库；提交与更正事务内先
  校验后写入，失败整体回滚，因此不存在半成品数据。查数与更正都只针对该
  状态，三种口径互斥且合计等于该卡已收批数。
- **提交**：批号唯一；异常项目必须在所用卡的实检清单内。任何一项在清单
  外（或批号重复）则整批退回，不写批次、不写明细、不动计数。
- **更正**：替换该卡实检清单。原 `batch_abnormalities` 明细行一字不改；
  按新清单与"已收批数"重算该卡固定大小的计数行：
  - 保留在清单内的项：有异常数不变，已检无异常 = 已收批数 − 有异常数
  - 移出清单的项：有异常 0、已检无异常 0、未检 = 已收批数
  - 新增清单的项：有异常 0、已检无异常 = 已收批数
- **整次退回**：更正若会让任何一条已存异常明细落到清单外，整卡不写入。
  提交同理。数据库触发器再兜底这两条不变量。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 下串行（SQLite 单写者 + busy
  timeout），后到者基于前一个已提交状态校验，因此并发也不会留下清单外的
  异常或半批数据。
- **读写量恒定**：计数物化在 `card_item_stats`，行数 = 卡数 × 项目数。
  提交/更正只写该卡的固定行；查数按 `(item_id)` 索引读每卡一行。均不随
  已收批数增长（`tests/test_ledger.py` 用 SQLite progress-handler 精确
  计数验证 n=2 与 n=302 时指令数完全相等）。

## 测试

```bash
python3 -m pytest -q
python3 scripts/http_smoke.py http://127.0.0.1:8000   # 对运行中的实例，空库
```

`http_smoke.py` 只走 HTTP：登记清单、提交、比对更正前后的三口径、验证
整批/整次退回、并发抢同一批号只有一条入库，最后在 `/audit` 确认三处
不一致计数均为 0。

## 目录

```
app/
  __init__.py      应用工厂
  db.py            SQLite 连接、建表、多 worker 安全初始化
  schema.sql       表、索引、两个不变量触发器
  seed.py          固定的卡与项目字典，预建全部计数行
  service.py       提交 / 更正 / 查数 / 核对（全部业务规则）
  routes.py        HTTP 路由
  templates/       页面
  static/          样式与异常项目联动脚本
tests/test_ledger.py
scripts/http_smoke.py
Dockerfile, docker-compose.yml, requirements.txt
```

## 已知限制

- 未实现登录与权限；任何能访问页面的人都能提交或更正。
- 卡与项目字典固定写在 `seed.py`，页面不提供增删。
- 只有提交与作废之外的单一状态；没有历史版本链，更正不保留清单快照
  （原异常明细保留，但旧清单本身不另存）。
- `/audit` 与 `scripts/http_smoke.py` 会扫描批次，仅用于核对，不在
  查数/更正路径上。
- 本环境没有 Docker 守护进程，`docker compose up` 未能实际执行；详见
  交付说明。
