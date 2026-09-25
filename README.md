# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

实验室异常调查单独成模块：首检不合格自动建立待查记录，必须登记异常原因、复验方案和负责人；复验结果回写调查时间线，提交结论后由另一名质量人员确认才能关闭。调查未关闭（含待确认、关键偏差未处理）时正式放行被阻断。检验新增轮次、返工或供应商资料变更后，原调查归档并按新版本重新判定，历史版本留档。

## 模块分层（分别维护）

- `investigations/rules.py`：纯规则，无数据库/HTTP，判断阻断原因与结论前置条件。
- `investigations/store.py`：调查、版本归档、事件时间线的表结构与存储。
- `investigations/service.py`：调查工作流、角色校验、版本并发与放行闸门。
- `app.py`：批次/偏差/检验等主服务，并在检验、返工、供应商变更处挂调查钩子。
- `static/investigations.html`：调查页面，独立于批次总览页 `static/index.html`。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。调查页面地址 `/investigations`。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次；首检不合格自动建调查。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工（触发调查重判）。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录（供应商变更触发调查重判）。
- `GET /api/investigations`、`GET /api/investigations/{id}`：待查/已关闭列表与详情（含最新检验、阻断原因、时间线、历史版本）。
- `POST /api/investigations/{id}/register`：登记异常原因、复验方案、负责人（lab/inspector/qa）。
- `POST /api/investigations/{id}/conclude`：复验合格回写后提交结论（可标记关键偏差）。
- `POST /api/investigations/{id}/confirm`：由另一名 qa 确认结论并关闭（不能与提交人为同一人）。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查；调查未闭环时正式放行被阻断。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情（含调查与放行阻断项）、状态和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差、例外有效期和实验室异常调查闭环为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
