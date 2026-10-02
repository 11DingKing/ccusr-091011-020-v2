# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 入库记录更正（追加式更正链）

入库记录的敏感字段（批次号、入库数量）不允许直接覆盖，一律通过追加式更正单变更，
主记录只保存最新生效值，完整历史由更正链重建：

- `POST /api/stock-in/<id>/corrections/` 提交更正单（记录原值、建议值、理由、提交人）
- `POST /api/stock-in/corrections/<id>/approve/` 批准生效（批准人不能是提交人）
- `POST /api/stock-in/corrections/<id>/reject/` 拒绝；`.../withdraw/` 提交人撤回
- `POST /api/stock-in/corrections/<id>/reverse/` 撤销已生效更正（生成反向分录，仍需审批）
- `GET /api/stock-in/<id>/corrections/` 查看完整更正链
- `GET /api/stock-in/?as_of=<ISO时间>` 按时间点重建旧视图（默认返回最新状态）
- `GET /api/export/?type=stock_in_correction` 导出更正链，解释主记录前后差异

规则：同一记录同一时刻仅允许一笔待审批更正；审批时校验原值与当前值一致，
过期提案须重新提交；已生效更正不可删除，只能撤销一次且撤销分录不可再被撤销；
记录已被后续业务引用（入库日期已生成报表）时数量更正须管理员批准，
数量调减不得使库存为负（即不得冲减已被后续出库消耗的数量）。
审批生效与主记录、库存、更正链的写入在同一事务内完成，任何失败整体回滚。

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
