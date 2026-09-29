# 后端测试说明

## 怎么跑

在项目根目录执行：

```powershell
python -m pytest backend/tests -q
```

`pytest.ini` 已配置 `backend/` 为 import 根路径，并默认收集 `backend/tests`。
前端测试在 `frontend/` 下执行 `npm run test:unit`。

## 数据库测试隔离

默认情况下，测试会读取 `.env` 中的数据库配置。执行需要真实 PostgreSQL 的测试前，
请使用专用开发库或隔离容器，不要指向生产库：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://<用户>:<密码>@localhost:55432/data_platform"
python -m pytest backend/tests -q
```

大多数测试是纯函数、模型元数据或 Agent 节点测试，不调用外部模型，也不需要网络。
历史数据域测试已移到 `legacy/tmall/tests/`，不会被默认 pytest 收集。

## 测试目录约定

- `test_data_query_graph.py`：问数 Agent、工具边界、SQL 修复和图表建议。
- `test_knowledge_chunking.py`：电商经营知识文档切片与稳定性。
- `test_safe_query_validation.py`：SQL AST、字段白名单和只读查询边界。
- `test_runtime_modes.py`：Demo/Real 运行模式与初始化契约。

测试中需要定位文件时使用 `Path(__file__)`，不要依赖某个特定的当前工作目录。
