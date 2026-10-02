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

## 入库敏感字段更正（追加式更正分录）

入库记录的**入库数量**与**批次号**不再允许直接覆盖。任何修改都通过只追加、
不可变的更正分录链（`StockInCorrection`）完成，主记录 `quantity`/`batch_no`
永远等于“初始登记值 + 全部已生效且未撤销的更正分录”的重建结果。

### 分录结构

每笔分录记录：字段、原值、建议值、理由、申请人/申请时间、批准人/生效时间、
状态（待批准/已生效/已拒绝）、拒绝理由；撤销时记录被撤销的目标分录；被下游
业务引用时冻结下游出库引用快照。

### 业务规则

1. **两阶段生效**：提议（`proposed`）不改变主记录；只有批准后才在同一事务内
   更新主记录（数量更正同时联动库存结余，结余不得为负）。申请人不能批准本人分录。
2. **连续更正**：按 `(入库记录, seq)` 形成线性链；同一字段存在待批准分录时禁止
   再次提议；批准时校验分录原值等于主记录当前值，防止并发覆盖。
3. **撤销更正**：只能撤销该字段**最新一笔**已生效且未被撤销的更正；撤销本身
   也是一笔追加分录，需重新提议与批准。更早的更正通过新增更正处理。
4. **下游引用**：入库登记之后存在非“已拒绝”状态的出库记录时，更正/撤销须由
   管理员批准，且冻结引用快照供审计。
5. **原子性**：提议、批准、拒绝、撤销全部在单事务内完成（主记录与分录行加锁），
   任何校验失败整体回滚，并在生效后自检主记录可被更正链完整重建，绝不留下
   “主记录已改、更正链缺失”的状态。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/stock-in/` | 入库初始登记（写入初始值与库存） |
| GET | `/api/stock-in/` | 入库列表，默认最新状态；`as_of=YYYY-MM-DD` 或 ISO 时间可重建该时点旧视图 |
| GET | `/api/stock-in/{id}/corrections/` | 更正分录链（按序号升序） |
| POST | `/api/stock-in/{id}/corrections/` | 提议更正：`field_name`、`new_value`、`reason` |
| POST | `/api/corrections/{id}/approve/` | 批准并生效 |
| POST | `/api/corrections/{id}/reject/` | 拒绝（需 `reject_reason`） |
| POST | `/api/corrections/{id}/reverse-proposal/` | 提议撤销一笔已生效更正（需 `reason`） |

`/api/daily-report/`、`/api/dashboard/`、`/api/export/?type=stock_in` 均支持
`as_of` 时间点参数；`/api/export/?type=stock_in_corrections` 导出完整更正链审计表。

