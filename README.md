# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/liability_determination/`：事故案件与当事方、当事人陈述/车辆轨迹/现场证据/法规依据汇总、责任认定草案与不可变版本历史、主办提交—复核人签署—负责人终审、生效与送达文本；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
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
PYTHONPATH=src python3 -m liability_determination.acceptance
PYTHONPATH=src python3 -m penalty_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度、证据复核以及责任认定的提交、多级签署、修订与送达文本生成，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## 责任认定流程

`liability_determination` 把事故案件、当事方和四类材料（当事人陈述 `statement`、车辆轨迹 `trajectory`、现场证据 `scene`、法规依据 `regulation`）汇总为可复核的责任认定结论：

1. 主办民警（`investigator`）登记案件与当事方、录入材料，建立责任认定草案（`POST /cases/{id}/draft`）；草案提交前可反复保存（`PUT /cases/{id}/draft`）。
2. 主办提交（`POST /cases/{id}/submit`）后进入签署流程，版本不可再改。
3. 复核人员（`reviewer`）签署 `review`；当案件涉及人员伤亡（`casualties`）或证据链异常（`evidence_anomaly`）时，还须负责人（`chief`）完成 `final` 终审。签署人不能是主办本人，且终审前必须先有复核同意。
4. 只有当前版本完成所需层级的全部同意签署后才生效，并生成唯一的《道路交通事故责任认定书》送达文本。
5. 驳回会把当前版本置为 `rejected`，主办可据此修订；补充证据或修改责任比例通过修订（`POST /cases/{id}/revise`，必填 `change_summary`）生成新版本。旧版本、曾经的签署意见和送达文本一律保留，仅把旧签署与旧送达文本标记为 `superseded`，不删除任何意见。
6. 同一层级重复签署幂等（不产生第二份决定；意见已形成时不可更改），修订会重新走完整签署链。
7. 查询（`GET /determinations/{case_id}`、`/determinations/{case_id}/{version_no}`）返回每一方的责任比例、定性与理由、版本引用的材料、各签署层级状态、完整状态变化时间线和版本历史；`GET /cases/{id}/audit` 返回审计事件。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m liability_determination.api --database liability.sqlite3 --host 127.0.0.1 --port 8083
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
```

四个服务均提供 `GET /health`，其余接口使用 JSON（责任认定等写接口通过 `X-Actor-Id` 标识操作人）。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。
