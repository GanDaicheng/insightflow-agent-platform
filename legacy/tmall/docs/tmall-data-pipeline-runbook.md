# 天猫 IJCAI 2015 数据接入运行手册

本文是**操作手册**：怎么建表、怎么导数据、怎么验证、出问题怎么查。
口径与限制的解释见 `backend/knowledge_seed/tmall/` 下的四份知识文档。

---

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| PostgreSQL | 可用，且项目根 `.env` 里的 `DATABASE_URL` 指向它 |
| Python 环境 | 已安装 `backend/requirements.txt` |
| 原始数据 | `data_format1.zip`（约 360MB） |
| 磁盘 | **不需要**额外 2GB 解压空间——导入全程直接读 ZIP |

目录内的四个文件：

```
data_format1/user_info_format1.csv   用户画像
data_format1/user_log_format1.csv    行为日志（约 1.9GB 解压后）
data_format1/train_format1.csv       复购训练样本
data_format1/test_format1.csv        复购测试样本
```

---

## 2. 建表（迁移）

```bash
cd backend
python -m alembic upgrade head
```

这一步只建表，**不导入任何数据**。结构变更与数据装载刻意分开：
迁移要能反复、快速、幂等地跑，而导入要读 360MB 压缩包、跑几分钟。

确认迁移没漏：

```bash
python -m alembic current      # 应显示 bea2b5793f31 (head)
python -m alembic heads        # 应只有一行 head
```

**回滚**（会删掉全部天猫表和数据）：

```bash
python -m alembic downgrade -1
```

---

## 3. 先跑 dry-run

**永远先跑 dry-run。** 它只读 ZIP、校验每一行、统计行数，
**一次数据库调用都没有**——所以数据库没起来时也能跑。

```bash
cd backend
python scripts/ingest_tmall_data.py \
  --zip <path-to>/data_format1.zip \
  --dry-run
```

它会输出：

- ZIP 的 SHA256；
- 四个文件各自的原始行数 / 抽样后行数；
- 四种行为的分布；
- 与登记基线的逐项对照（哪些一致、哪些不一致）。

预期输出（默认参数 `--sample-modulus 55 --sample-residue 0`）：

| 项 | 原始行数 | 抽样行数 |
| --- | --- | --- |
| user_info | 424,170 | **7,712** |
| user_log | 54,925,330 | **998,542** |
| train | 260,864 | **4,700** |
| test | 261,477 | **4,858** |

动作分布：click 881,857 / cart 1,343 / favorite 55,306 / buy 60,036。
日期范围：2014-05-11 ~ 2014-11-12。

**任何一项与基线不一致，就不要继续。** 先查清楚是换了抽样参数，
还是数据/解码逻辑出了问题。

dry-run 需要完整读一遍 5492 万行，大约 2~3 分钟。

---

## 4. 正式导入

```bash
cd backend
python scripts/ingest_tmall_data.py \
  --zip <path-to>/data_format1.zip \
  --sample-modulus 55 \
  --sample-residue 0 \
  --batch-size 10000
```

`--batch-size` 只控制**进度上报间隔**，不影响内存占用，也不影响结果。
记录是逐条流给 PostgreSQL 的，不存在「攒够一批再写」这个动作。

### 目标表已有数据时

默认**拒绝覆盖**：

```
导入未执行：
  目标表已存在数据，未指定 --replace。（tmall_users 7712 行、…）
  如需覆盖请显式指定 --replace。
```

确认要覆盖时加 `--replace`：

```bash
python scripts/ingest_tmall_data.py --zip <path> --replace
```

`--replace` 会先清空三张 Silver 表再重新导入，并整表重算 Gold。
它同时会**跳过幂等判断**——显式要求覆盖时不该得到一句「已跳过，什么也没做」。

### 重复导入会被自动跳过

同一份 ZIP（按 SHA256 识别）配同一组抽样参数，成功导入过之后再跑一次：

```
已跳过：同一文件与抽样参数此前已成功导入，本次未改动任何数据。
```

跳过需要**同时**满足两个条件：台账里有成功的记录，**且**目标表里确实有数据。
只看台账的话，有人手工清过表之后会得到「已导入，跳过」——留下一个空库，
而且看起来完全成功。

### 耗时参考

| 阶段 | 耗时 |
| --- | --- |
| SHA256 | 约 1 秒 |
| 读 ZIP + 解码 + 校验 | 约 2~3 分钟 |
| COPY 写入（101 万行） | 约 10 秒 |
| Gold 整表重算 | 约 5 秒 |

瓶颈是 Python 侧的 CSV 解码，不是数据库。

---

## 5. 导入后验证

```bash
cd backend
python scripts/verify_tmall_data.py
```

七项检查：

| 检查项 | 内容 |
| --- | --- |
| 数据规模 | 用户 / 事件 / 训练集 / 测试集行数 vs 基线 |
| action_type 分布 | 四个动作都要出现，且与基线一致 |
| 日期范围 | 必须落在 2014-05-11 ~ 2014-11-12 内 |
| 孤儿记录 | 事件表 / 样本表里的用户必须都能在用户表里找到 |
| Gold/Silver 一致性 | 六张 Gold 表汇总出来的数必须等于 Silver 明细的数 |
| 抽样一致性 | 各表用户都必须满足 `user_id % modulus == residue` |
| 重复运行幂等 | 加 `--check-idempotency --zip <path>` 时执行 |

全部通过时退出码 0，任意一项不通过为 1，可以直接接进 CI。

验证「重复运行是安全的」（会真的跑一次导入，但正常情况下会被跳过）：

```bash
python scripts/verify_tmall_data.py --check-idempotency --zip <path>/data_format1.zip
```

换过抽样参数时加 `--sample-modulus` / `--sample-residue`。
未登记的参数下，规模断言会显示为「跳过」——那不是失败，
参数是调用方的自由选择，只是没有基准线可比。

### 端到端冒烟（不写库、跑完回滚）

想验证「COPY、Gold SQL、约束」这一整条真实路径，用冒烟脚本：

```bash
cd backend
python scripts/smoke_tmall_pipeline.py
```

它在一个事务里建临时 schema、跑完整导入、断言结果，**最后整体回滚**。
跑完数据库和执行前完全一样，所以可以随时执行，不需要先迁移。

---

## 6. 知识库入库

天猫的四份知识文档在 `backend/knowledge_seed/tmall/`。
沿用项目现有的切片 / Embedding / pgvector 流程，**没有第二套向量系统**：

```bash
cd backend
python scripts/ingest_knowledge.py --dry-run     # 先看会做什么，不花钱
python scripts/ingest_knowledge.py               # 正式入库
```

默认目录已从 `knowledge_seed/retail` 改为 `knowledge_seed`，
扫描是**递归的**，所以零售与天猫两边的文档一次入库。

幂等：文档没变整篇跳过；变了只重算变化的那几个切片。

---

## 7. 出问题怎么查

### 先看导入台账

```sql
SELECT run_id, file_name, status, sample_modulus, sample_residue,
       raw_row_count, imported_row_count,
       started_at, finished_at, error_category, error_message
FROM tmall_ingestion_runs
ORDER BY run_id DESC
LIMIT 10;
```

台账表**刻意没有唯一约束**：它是运行历史，同一份文件失败两次、重试三次
都应该各留一条记录。失败的尝试也会留下痕迹。

### 错误分类对照

`error_category` 是受控枚举，`error_message` 是固定的中文说明，
**不含任何数据库连接信息**。

| category | 含义 | 怎么处理 |
| --- | --- | --- |
| `header_mismatch` | CSV 表头与登记不一致 | 确认是不是换了数据集版本 |
| `row_shape` | 某行列数与表头不符 | 文件被截断或被手工改过 |
| `field_type` | 字段无法解析成期望类型 | 同上 |
| `invalid_date` | `time_stamp` 不是合法的 MMDD | 数据损坏 |
| `invalid_action_type` | `action_type` 不在 0~3 | 数据损坏或换了数据集 |
| `duplicate_key` | 主键/唯一键重复（SQLSTATE 23505） | 并发导入；检查是否有另一个进程在跑 |
| `constraint_violation` | 违反 CHECK 约束（SQLSTATE 23514 等） | 解码逻辑被改坏了 |
| `existing_data_present` | 目标表已有数据且没给 `--replace` | 加 `--replace` 或先清表 |
| `database_error` | 其他数据库错误 | 看 PostgreSQL 日志 |

分类用的是 **SQLSTATE** 而不是异常类名：SQLAlchemy 会把 asyncpg 的具体异常
统一包成 `IntegrityError`，类名分不出「主键重复」和「CHECK 约束不满足」，
而这两种错的排查方向完全不同。

### 事务语义

导入分成三个事务：

| 事务 | 内容 | 提交时机 |
| --- | --- | --- |
| A | 插入一行 `status='running'` 的台账 | 立即独立提交 |
| B | **清空旧 Silver** + 写 Silver + 重算 Gold + 标记 `succeeded` | 全部成功才提交 |
| C | 标记 `failed` + 错误分类 | 仅失败时执行 |

拆开的原因是：主事务回滚时，那条「这次尝试失败过」的台账行**也会被一起回滚**。
拆开之后，失败的导入在数据上干干净净（Silver 与 Gold 回到导入前），
但在台账里留下完整记录。

**清空旧数据必须待在 B 里面，不能在 B 前面单独提交。**
`--replace` 时如果单独提交一次清空，中途失败会留下
「旧数据已删、新数据回滚」的状态——Silver 空、Gold 还是上一批的，
而且旧数据**已经不可恢复**。放进 B 之后，失败时 PostgreSQL
会把删除也一起撤掉。

#### 一个必须显式处理的坑：asyncpg 的隐式事务

SQLAlchemy 的 asyncpg 方言不下发显式 `BEGIN`，靠 asyncpg 的隐式事务。
于是「SQLAlchemy 认为在事务里」**不等于**「数据库真的在事务里」：

```
conn.in_transaction()       → True
asyncpg.is_in_transaction() → False
```

如果事务里的第一个动作是绕过 SQLAlchemy 直接调的 asyncpg COPY，
这次 COPY 会在**自动提交**模式下执行，写进去的数据再也回滚不掉——
而 SQLAlchemy 的回滚会「成功」返回，没有任何报错。

所以每次写事务开头都会先走 `ensure_write_transaction`（`SELECT 1` +
断言 asyncpg 确实在事务里），`copy_records` 里也有一道兜底检查。
这不是理论风险，是实测出来的：修之前 `--replace` 中途失败会留下
「旧数据没了、新数据只进来一半」。

**回归测试**：`backend/tests/test_tmall_ingest_integration.py`
预先放好旧 Silver 与旧 Gold，制造中途失败，断言旧数据一个字节没变。
它需要真实 PostgreSQL；连不上时整个文件自动跳过。

```bash
cd backend
python -m pytest tests/test_tmall_ingest_integration.py -v
```

### 表结构与模型对不上

```bash
cd backend
python -m alembic check          # 需要 alembic 1.9+
python -m alembic upgrade head --sql | tail -40    # 离线渲染，不写库
```

---

## 8. 常用命令速查

```bash
# 建表
cd backend && python -m alembic upgrade head

# 只看不写
python scripts/ingest_tmall_data.py --zip <path> --dry-run

# 正式导入
python scripts/ingest_tmall_data.py --zip <path> \
    --sample-modulus 55 --sample-residue 0 --batch-size 10000

# 覆盖重导
python scripts/ingest_tmall_data.py --zip <path> --replace

# 验证
python scripts/verify_tmall_data.py

# 验证 + 幂等
python scripts/verify_tmall_data.py --check-idempotency --zip <path>

# 端到端冒烟（回滚，不留痕迹）
python scripts/smoke_tmall_pipeline.py

# 换一组抽样参数
python scripts/ingest_tmall_data.py --zip <path> \
    --sample-modulus 11 --sample-residue 3
```

---

## 9. 后台表之间的取舍

### Silver 表之间没有外键

`tmall_user_events` 与 `tmall_users` 之间刻意**不建外键**：

- 抽样按 `user_id` 取模、四个文件用同一条规则，理论上不会产生孤儿。
  但「理论上不会」正是需要校验的地方，而外键会把一个**可观测的差异**
  变成一次导入失败——排查时我们更想看到「有 3 个孤儿」而不是「COPY 失败」。
- 事件表有 100 万行，外键检查要在每次写入时逐行维护。

所以用校验查询代替外键，`verify_tmall_data.py` 会检查孤儿记录。

### Gold 层每次整表重算

每次导入都在同一个事务里先清空 Gold、再全量重算。理由不是「简单」：

- 增量更新要先解决「哪条明细属于哪次导入」，而抽取范围由抽样参数决定。
  换一组参数重导，增量逻辑就得先撤销上一批的贡献——本质上还是全量重算，
  只是把复杂度藏起来了；
- 一致性只有全量重算能保证。增量算错一笔，错误会永远留在表里，
  而且没有任何断言能发现它；
- 成本可以忽略：101 万行聚合成六张 Gold 表，PostgreSQL 几秒钟。

### 智能问数只能查 Gold

`tmall_user_events` 等三张明细表**不在查询白名单里**。
这不是性能考虑（100 万行扫起来并不慢），而是权限边界：
只要明细表可查，任何 LLM 生成的 SQL 就能把某一个用户的完整行为轨迹拉出来。
留在白名单之外，「模型能不能看到用户级明细」这个问题在表这一层就有答案。

代价是明细的灵活性没了。这份数据集没有 session，本来也不支持路径分析，
所以不算损失。

### 不允许跨领域 JOIN

天猫表与零售表不能出现在同一条 SQL 里。两边的时间不重叠、主体不重叠、
口径也不通用，连起来的数字没有业务含义——而且**不会报错**，
只是给出一个看起来正常的错数字。

拦截有三层：资产检索按领域过滤、Agent 的 SQL 校验、数据服务的
`safe_query` 白名单与跨领域规则。
