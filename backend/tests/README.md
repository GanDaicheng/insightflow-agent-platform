# 后端测试说明

## 怎么跑

在**项目根目录**执行：

```powershell
pytest -q
```

就这一条。不用先设 `PYTHONPATH`，也不要求在哪个目录下执行——
`pytest.ini` 已经把 import 根路径（`backend/`）和测试目录（`backend/tests`）都定好了，
所以从项目根目录或从 `backend/` 里跑，结果都一样。

前端的测试是另一套：在 `frontend/` 下用 `npm run test:unit`（vitest）。
两套 runner 刻意不合并，避免「一次失败到底是哪边的问题」难以判断。

## ⚠️ 跑测试前必须先隔离数据库

**默认情况下，测试读的是项目根 `.env` 里的 `DATABASE_URL`，也就是你的开发库。**
要在隔离库上跑，用环境变量覆盖（环境变量优先级高于 `.env`）：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://<用户>:<密码>@localhost:55432/data_platform"
pytest -q
```

`localhost:55432` 是隔离库的示例端口。**指向哪个库由你决定，但不要指向生产库。**

为什么必须强调这件事：`backend/tests/test_tmall_ingest_integration.py` 会真的连数据库、
真的建表、真的跑 COPY。它连不上时才会整文件跳过——**能连上就会真跑**，
而它连的就是 `.env` 里那一个。

## 集成测试的 schema 隔离

`test_tmall_ingest_integration.py` 验证的是 PostgreSQL 的事务原子性：
`--replace` 导入中途失败时，旧数据必须一个字节都不变。这类语义**无法用桩验证**，
所以它需要一个真数据库。

隔离方式：

| 机制 | 做法 |
| --- | --- |
| 独立 schema | 每次运行生成唯一名字 `tmall_itest_<8位随机>`，不碰 `public` |
| 连接级 `search_path` | 只设测试 schema，**不带 `public`**（原因见下） |
| 建表 | 用 `Base.metadata.create_all()` 只建 tmall 相关的表 |
| 清理 | 每个场景结束时 `DROP SCHEMA ... CASCADE`，schema 名唯一所以崩溃残留也不会影响下次 |

### 为什么 `search_path` 里绝对不能带 `public`

这一条踩过坑，写在这里免得被"改回去"：

`search_path` 曾经是 `"<测试schema>, public"`，看起来更保险（找不到就去 public 兜底）。
但 `create_all()` 在每张表创建前会先调 `has_table()` 检查存在性——
`public` 在搜索路径里时，只要 `public.tmall_users` 已存在（**任何迁移过的库都是这种情况**），
`has_table()` 就返回真，`create_all()` 于是认为"这张表已经有了"，**跳过创建**。

结果是测试 schema 里一张表都没建，而后面所有不带 schema 限定的 INSERT
就顺着 `search_path` 落到了 `public` ——**真实数据上**。表现出来是主键冲突，
或者更糟：静默改写了生产表。

去掉 `public` 之后，`has_table()` 只看得到测试 schema，建表正常发生。
测试是否安全，不再取决于"目标库的 public 恰好是空的"，而是无条件成立。

## 测试对工作目录的假设

**不要**在测试里写 `Path("backend/app/...")` 这种相对项目根目录的路径——
它只有在 cwd 恰好是项目根时才成立。用 `__file__` 定位：

```python
SOURCE = Path(__file__).resolve().parents[1] / "app" / "..."   # backend/ 下的文件
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "..." # tests/ 下的夹具
```

`parents[1]` 是 `backend/`（因为本文件在 `backend/tests/`），`parent` 是 `backend/tests/`。
