# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。首检不合格自动建立实验室异常调查，登记原因、复验方案和负责人，复验结果回写调查；修改检验、返工或供应商资料后，调查按新版本重判，旧记录留档。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差或未关闭的调查始终阻止正式放行，调查结论须由另一名质量人员确认，修改必须携带当前批次修订号。

放行规则在 `rules.py`，调查存储在 `investigations.py`，页面在 `static/index.html`，三者分开维护。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次；首检不合格自动建立待查调查，复验结果回写调查。
- `POST /api/investigations/{id}/register`：登记调查原因、复验方案和负责人。
- `POST /api/investigations/{id}/conclude`、`POST /api/investigations/{id}/confirm`：提交调查结论，由另一名质量人员确认关闭。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查；状态中含调查列表（待查/已关闭、最新检验与阻断原因），首页分区展示并可展开。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
