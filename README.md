# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
- `src/liability_determination/`：责任认定草案与版本历史、分级签署（主办提交、复核签署、负责人终审）、旧签署失效留痕、生效控制、送达文本与责任解释查询；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m traffic_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m evidence_review.acceptance --workspace .
PYTHONPATH=src python3 -m penalty_ops.acceptance
PYTHONPATH=src python3 -m liability_determination.acceptance --workspace .
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度、证据复核与责任认定分级签署（含退回修订、生效后改定责任比例、送达文本幂等生成），并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## 责任认定流程

责任认定以事故案件为单位建立唯一草案，草案下保留不可删除的版本历史：

1. 主办民警（`investigator`）汇总当事人陈述、车辆轨迹、现场证据和法规依据四类材料，为每一方填写责任比例（合计 100%）与认定理由，形成草案第 1 版；
2. 主办民警提交（`submit` 签署）后，由另一名复核人员（`reviewer`，不得是提交人本人）签署同意或退回修订；
3. 案件涉及人员伤亡或证据链异常时，复核通过后还须由负责人（`chief`）终审；
4. 当前版本完成全部所需层级才进入 `effective`，此后才能生成送达文本；送达文本按版本只生成一份，重复获取返回同一决定；
5. 补充证据或修改责任比例必须修订形成新版本：旧版本标记为 `superseded`，旧签署标记为 `invalidated`（原始意见、签署人与失效原因永久保留），新版本重新走所需层级；复核退回后的修订必须携带 `review_returned` 原因；
6. `GET /drafts/{draft_id}/explanation` 解释每一方责任比例与中文责任等级、按四类归集的引用材料，以及按时间排列的状态变化记录。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m liability_determination.api --database liability.sqlite3 --host 127.0.0.1 --port 8083
```

四个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。
