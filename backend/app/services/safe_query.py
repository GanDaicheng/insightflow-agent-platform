"""受控只读 SQL 查询服务（数据中台的数据服务层）。

这是**数据中台**的能力，因此刻意不导入 app.agent、LangGraph、LangChain 或任何模型 SDK：
数据中台不能反向依赖 AI 中台。Agent 里那份 sql_validation.py 面向「LLM 草稿的修复」，
和这里的取舍不同，两份策略目前各自独立，将来若需要统一再单独做共享模块重构。

两道防线（第 1 道不够时第 2 道兜底，缺一不可）：

    调用方 SQL
    → 第 1 道：sqlglot AST 校验（本模块 validate_safe_select）
       —— 用语法结构判断「这条 SQL 长什么样」，字符串匹配绕得过去，AST 绕不过去
    → 第 2 道：PostgreSQL 只读事务 + statement/lock timeout（本模块 run_readonly_query）
       —— 即使第 1 道将来出现漏洞，数据库层也不会真的写入，且不会被慢查询拖死
    → PostgreSQL
    → 限制行数的结构化结果

安全约定：
- 响应与日志都不回显原始 SQL、连接串、数据库异常原文；
- issues 只使用固定文案，不拼接任何调用方输入；
- 未知的字段类型不会静默 str() 伪装成功，而是抛受控异常。

本地开发原型说明：本服务当前**没有身份认证、没有权限控制、没有行级数据权限**，
只适合在可信的本地开发环境使用。生产环境必须在网关或本层补上身份认证、
按用户的表/字段授权与行级过滤，并把 SQL 审计写入独立的审计通道。
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.repositories.database import get_engine
from app.services.data_domains import cross_domain_violation

logger = logging.getLogger(__name__)

# 固定用 PostgreSQL 方言解析。换方言会改变语法规则，校验结论就不可信了。
DIALECT = "postgres"

MAX_SQL_LENGTH = 10_000
MIN_LIMIT = 1
MAX_LIMIT = 200

# 结果最多读 200 行。LIMIT 已经限制在 200 以内，这里是第二道保险：
# 万一校验出现漏洞放进来一条不设限的查询，也不会把整张表拉进内存。
MAX_RESULT_ROWS = 200

STATEMENT_TIMEOUT_MS = 3_000
LOCK_TIMEOUT_MS = 1_000


# --------------------------------------------------------------------------
# 白名单：表与字段
# --------------------------------------------------------------------------

# 默认拒绝：只有在这里登记过的表才能被查询。
# information_schema / pg_catalog 以及任何未登记的表都不在字典里，因此天然被拒。
#
# 字段是显式登记的，而不是从 ORM 模型自动推导：自动推导意味着将来给某张表加了
# 敏感列（比如手机号），它会立刻对所有调用方可见。显式登记强迫每次扩权都经过一次
# 有意识的修改，测试 test_whitelist_matches_models 会保证登记内容与模型不脱节。
ALLOWED_COLUMNS: dict[str, frozenset[str]] = {
    # ---- 零售领域 ----
    "customers": frozenset({"customer_id", "member_level"}),
    "products": frozenset({"product_id", "product_name", "category_name", "unit_price", "cost_price"}),
    "regions": frozenset({"region_id", "region_name"}),
    "date_dim": frozenset({"date_id", "full_date", "year", "quarter", "month"}),
    "orders": frozenset(
        {
            "order_id",
            "order_no",
            "customer_id",
            "product_id",
            "region_id",
            "date_id",
            "quantity",
            "gross_amount",
            "discount_amount",
            "net_amount",
        }
    ),
    "channels": frozenset({"channel_id", "channel_name", "channel_type"}),
    "promotions": frozenset(
        {
            "promotion_id", "promotion_name", "promotion_type", "start_date_id", "end_date_id",
            "discount_rate", "budget_amount",
        }
    ),
    "order_operations": frozenset(
        {
            "order_no", "channel_id", "promotion_id", "warehouse_province", "shipped_date_id",
            "delivered_date_id", "delivery_days", "shipping_fee", "fulfillment_status",
        }
    ),
    "inventory_snapshots": frozenset(
        {
            "snapshot_date_id", "product_id", "region_id", "opening_stock", "inbound_qty",
            "sold_qty", "ending_stock", "stockout_flag",
        }
    ),
    "ad_campaigns": frozenset(
        {
            "campaign_id", "campaign_name", "channel_id", "product_id", "start_date_id",
            "end_date_id", "budget_amount",
        }
    ),
    "ad_daily_metrics": frozenset(
        {
            "campaign_id", "date_id", "spend_amount", "impressions", "clicks", "conversions",
            "attributed_sales_amount",
        }
    ),
    "after_sales": frozenset(
        {
            "after_sale_id", "order_no", "after_sale_type", "reason", "status", "refund_amount",
            "request_date_id", "completed_date_id",
        }
    ),
}

# 只允许这几个聚合函数。sqlglot 会把已知函数解析成专门的节点类
# （version() → CurrentVersion，CASE → Case/If），未知函数落到 Anonymous，
# 它们全都是 exp.Func 的子类，所以「不是这几个类就拒绝」一条就能全覆盖。
ALLOWED_FUNCTIONS: tuple[type[exp.Func], ...] = (
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
)

_AGGREGATE_FUNCTIONS: tuple[type[exp.Func], ...] = ALLOWED_FUNCTIONS


# --------------------------------------------------------------------------
# 固定文案：不含任何调用方输入
# --------------------------------------------------------------------------

ISSUE_EMPTY = "SQL 不能为空。"
ISSUE_TOO_LONG = "SQL 长度超出限制。"
ISSUE_COMMENT_OR_MULTI = "查询不能包含注释或多条语句。"
ISSUE_SINGLE_SELECT = "仅允许执行单条 SELECT 查询。"
ISSUE_PARSE = "SQL 无法解析为合法的 PostgreSQL 查询。"
ISSUE_SELECT_STAR = "禁止使用 SELECT *，请显式列出字段。"
ISSUE_UNAUTHORIZED_TABLE = "查询引用了未授权的数据表。"
ISSUE_UNAUTHORIZED_COLUMN = "查询引用了未授权的字段。"
ISSUE_UNQUALIFIED_COLUMN = "字段必须写完整表名，例如 orders.net_amount。"
ISSUE_UNAUTHORIZED_FUNCTION = "查询使用了不允许的 SQL 函数。"
ISSUE_LIMIT = f"查询必须包含 LIMIT，且限制在 {MIN_LIMIT} 到 {MAX_LIMIT} 行之间。"
ISSUE_CTE = "禁止使用 WITH / CTE。"
ISSUE_SUBQUERY = "禁止使用子查询。"
ISSUE_SET_OPERATION = "禁止使用 UNION / INTERSECT / EXCEPT。"
ISSUE_WINDOW = "禁止使用窗口函数。"
ISSUE_CROSS_DOMAIN = "禁止跨领域关联查询：不同数据集的时间范围与业务口径互不通用。"


class SafeSqlValidation(TypedDict):
    """校验结果。issues 里只有固定文案，可以安全地返回给调用方。"""

    passed: bool
    issues: list[str]


# --------------------------------------------------------------------------
# 受控异常
# --------------------------------------------------------------------------


class SafeQueryError(Exception):
    """安全查询服务的受控异常基类。

    这些异常携带的都是可以安全外发的中文说明；数据库异常原文、SQL 原文
    只挂在 __cause__ 上，由 API 层丢弃，不会进入 HTTP 响应。
    """


class UnsafeSqlError(SafeQueryError):
    """SQL 未通过安全策略。对应 HTTP 422。"""

    def __init__(self, issues: Sequence[str]) -> None:
        super().__init__("SQL 未通过安全校验。")
        self.issues: list[str] = list(issues)


class DatabaseUnavailableError(SafeQueryError):
    """数据库不可用。对应 HTTP 503。"""

    def __init__(self) -> None:
        super().__init__("数据库暂时不可用，请稍后重试。")


class QueryExecutionError(SafeQueryError):
    """已通过安全校验，但语句本身执行失败（类型错误、超时被取消等）。对应 HTTP 400。"""

    def __init__(self) -> None:
        super().__init__("查询执行失败，请检查查询的字段类型与聚合写法。")


class ResultSerializationError(SafeQueryError):
    """查询结果无法安全序列化。属于服务端问题。对应 HTTP 500。"""

    def __init__(self) -> None:
        super().__init__("查询结果包含暂不支持的字段类型。")


class ResultTooLargeError(SafeQueryError):
    """返回行数超过服务上限。LIMIT 已被限制在 200 以内，走到这里说明校验被绕过。"""

    def __init__(self) -> None:
        super().__init__("查询返回的行数超出服务限制。")


# --------------------------------------------------------------------------
# 第 1 道防线：AST 校验（纯函数，不碰数据库）
# --------------------------------------------------------------------------


def _dedupe(items: list[str]) -> list[str]:
    """按出现顺序去重：同一类问题只报一次，读起来才不吵。"""
    return list(dict.fromkeys(items))


def _result(issues: list[str]) -> SafeSqlValidation:
    unique = _dedupe(issues)
    return {"passed": not unique, "issues": unique}


def _limit_clause_columns(tree: exp.Expression) -> set[int]:
    """LIMIT 子句里出现的标识符不算「字段引用」。

    例如 `LIMIT ALL` 会被 sqlglot 解析成一个名为 ALL 的列；如果不排除，
    issues 里就会冒出一条「字段必须写完整表名」，把人引到完全错误的方向。
    LIMIT 的合法性由下面单独一步判断。
    """
    limit_node = tree.args.get("limit")
    if limit_node is None:
        return set()
    return {id(column) for column in limit_node.find_all(exp.Column)}


def _projection_output_names(tree: exp.Select) -> set[str]:
    """投影列表里的输出名（显式 AS 别名，或裸字段投影的字段名）。"""
    names: set[str] = set()
    for projection in tree.expressions:
        if projection.alias:
            names.add(projection.alias.lower())
        elif isinstance(projection, exp.Column):
            names.add(projection.name.lower())
    return names


def _unqualified_allowed_ids(tree: exp.Select) -> set[int]:
    """可以不带表名前缀的列节点。

    ORDER BY / GROUP BY 引用输出别名（`ORDER BY sales_amount`）是标准写法，
    也是别名最主要的用途。这类标识符指向的是**已经校验过的投影**，
    不可能绕过白名单，所以放行。
    但仅限名字确实等于某个输出别名的情况：`ORDER BY created_at` 这种
    指向真实列却没写表名的写法仍然要拒绝。
    """
    allowed_names = _projection_output_names(tree)

    ids: set[int] = set()
    for key in ("order", "group"):
        clause = tree.args.get(key)
        if clause is None:
            continue
        for column in clause.find_all(exp.Column):
            if not column.table and column.name.lower() in allowed_names:
                ids.add(id(column))
    return ids


def _validate_limit(tree: exp.Expression, issues: list[str]) -> None:
    """LIMIT 必须存在，且是 1~200 的整数字面量。

    故意不替调用方补 LIMIT：补了会让调用方误以为自己的查询没有上限，
    实际情况却是被服务端悄悄改写了。
    """
    limit_node = tree.args.get("limit")
    if limit_node is None:
        issues.append(ISSUE_LIMIT)
        return

    value = limit_node.expression
    # 覆盖 LIMIT ALL、LIMIT 1+1、LIMIT ? 这些非字面量的写法
    if not isinstance(value, exp.Literal) or not value.is_int:
        issues.append(ISSUE_LIMIT)
        return

    amount = int(value.this)
    if amount < MIN_LIMIT or amount > MAX_LIMIT:
        issues.append(ISSUE_LIMIT)


def _validate_tables(tree: exp.Expression, issues: list[str]) -> dict[str, str]:
    """校验表名，并返回「限定前缀 → 真实表名」的映射（用于解析别名）。"""
    alias_to_table: dict[str, str] = {}

    for table in tree.find_all(exp.Table):
        # 任何 schema / 库名限定一律拒绝：information_schema、pg_catalog 都走这条
        if table.db or table.catalog:
            issues.append(ISSUE_UNAUTHORIZED_TABLE)
            continue

        name = table.name.lower()
        if name not in ALLOWED_COLUMNS:
            issues.append(ISSUE_UNAUTHORIZED_TABLE)
            continue

        # 登记表名本身，也登记它的别名，这样 o.net_amount 和 orders.net_amount 都能解析
        alias_to_table[name] = name
        if table.alias:
            alias_to_table[table.alias.lower()] = name

    return alias_to_table


def _validate_domains(tree: exp.Expression, issues: list[str]) -> None:
    """禁止跨领域 JOIN。

    为什么单列一条规则，而不是指望「白名单里没有那张表」把问题挡住？
    即使未来增加第二个业务域，也不能让两个无关业务域的表被拼在一起。
    这类错误的可怕之处是 SQL 可能不报错，只是给出一个看起来正常的错数字。

    未登记的表不参与判断：它们已经因为「未授权」被拒，再报一条跨领域
    只会让 issues 变长而不增加信息。
    """
    tables = {
        table.name.lower()
        for table in tree.find_all(exp.Table)
        if not table.db and not table.catalog
    }
    if cross_domain_violation(tables) is not None:
        issues.append(ISSUE_CROSS_DOMAIN)


def _validate_columns(
    tree: exp.Select, alias_to_table: dict[str, str], issues: list[str]
) -> None:
    """字段必须写完整表名（含别名），且必须在白名单内。"""
    limit_columns = _limit_clause_columns(tree)
    alias_reference_ids = _unqualified_allowed_ids(tree)

    for column in tree.find_all(exp.Column):
        if column.name == "*":
            continue  # SELECT * / orders.* 已单独报错
        if id(column) in limit_columns:
            continue  # LIMIT 子句单独判断
        if id(column) in alias_reference_ids:
            continue  # ORDER BY / GROUP BY 引用的是已校验过的输出别名

        if not column.table:
            issues.append(ISSUE_UNQUALIFIED_COLUMN)
            continue

        resolved = alias_to_table.get(column.table.lower())
        if resolved is None:
            continue  # 表本身已经报过「未授权」，不重复刷屏

        if column.name.lower() not in ALLOWED_COLUMNS[resolved]:
            issues.append(ISSUE_UNAUTHORIZED_COLUMN)


def _validate_projection(tree: exp.Select, issues: list[str]) -> None:
    """拒绝 SELECT *。只检查投影列表，COUNT(*) 里的 * 是合法聚合，不在范围内。"""
    for projection in tree.expressions:
        if isinstance(projection, exp.Star):
            issues.append(ISSUE_SELECT_STAR)
            return
        if isinstance(projection, exp.Column) and projection.name == "*":
            issues.append(ISSUE_SELECT_STAR)
            return


def _validate_functions(tree: exp.Expression, issues: list[str]) -> None:
    for node in tree.walk():
        # sqlglot models boolean connectors and arithmetic operators as
        # subclasses of Func too. They are expressions, not callable
        # database functions, so filtered metrics and AOV division remain
        # available.
        if isinstance(node, exp.Binary) and not isinstance(node, (exp.Case, exp.If)):
            continue
        if not isinstance(node, exp.Func):
            continue

        if isinstance(node, (exp.Case, exp.If)):
            # Conditional expressions are useful for safe conditional
            # aggregation, but a standalone CASE/IF remains disallowed.
            ancestor = node.parent
            while ancestor is not None and not isinstance(
                ancestor, _AGGREGATE_FUNCTIONS
            ):
                ancestor = ancestor.parent
            if isinstance(ancestor, _AGGREGATE_FUNCTIONS):
                continue

        if not isinstance(node, ALLOWED_FUNCTIONS):
            issues.append(ISSUE_UNAUTHORIZED_FUNCTION)
            return


def _validate_structure(tree: exp.Expression, issues: list[str]) -> None:
    """拒绝 CTE、子查询、集合运算、窗口函数。"""
    if any(isinstance(node, (exp.With, exp.CTE)) for node in tree.walk()):
        issues.append(ISSUE_CTE)

    if any(isinstance(node, exp.Subquery) for node in tree.walk()):
        issues.append(ISSUE_SUBQUERY)
    # FROM (SELECT ...) 之外，IN (SELECT ...) 也会生成嵌套的 Select 节点；
    # 直接数树里有几个 Select，比逐个列举写法更难漏。
    elif any(isinstance(node, exp.Select) and node is not tree for node in tree.walk()):
        issues.append(ISSUE_SUBQUERY)

    if isinstance(tree, exp.SetOperation):
        issues.append(ISSUE_SET_OPERATION)

    if any(isinstance(node, exp.Window) for node in tree.walk()):
        issues.append(ISSUE_WINDOW)


def validate_safe_select(sql: str) -> SafeSqlValidation:
    """校验一条待执行的 SQL 是否符合安全策略。

    纯函数：不连数据库、不发网络请求，因此可以在测试里高速穷举各种绕过写法。
    收集全部问题后再返回，而不是遇到第一个就退出。
    """
    issues: list[str] = []

    if not sql or not sql.strip():
        return _result([ISSUE_EMPTY])

    if len(sql) > MAX_SQL_LENGTH:
        issues.append(ISSUE_TOO_LONG)

    # 文本层只做「一眼可见」的快速拒绝，真正的判断在下面的 AST。
    # 分号与注释是多语句和注释注入最典型的载体，先挡掉成本最低。
    if ";" in sql:
        issues.append(ISSUE_COMMENT_OR_MULTI)
    if "--" in sql or "/*" in sql or "*/" in sql:
        issues.append(ISSUE_COMMENT_OR_MULTI)

    try:
        statements = [stmt for stmt in sqlglot.parse(sql, dialect=DIALECT) if stmt is not None]
    except ParseError:
        return _result(issues + [ISSUE_PARSE])

    if not statements:
        return _result(issues + [ISSUE_PARSE])

    # 多语句是最典型的注入入口：前半段看着正常，后半段才是目的
    if len(statements) != 1:
        issues.append(ISSUE_COMMENT_OR_MULTI)

    # 集合运算单独给一条更准确的说明，否则它会落到下面「不是 SELECT」那句泛化文案上
    if any(isinstance(stmt, exp.SetOperation) for stmt in statements):
        issues.append(ISSUE_SET_OPERATION)
        return _result(issues)

    # 任何一条不是 SELECT 就直接拒绝。DELETE / UPDATE / DROP 等都会落到这里。
    if any(not isinstance(stmt, exp.Select) for stmt in statements):
        issues.append(ISSUE_SINGLE_SELECT)
        return _result(issues)

    tree = statements[0]

    # sqlglot 会把注释挂到具体节点上，遍历整棵树找
    if any(node.comments for node in tree.walk()):
        issues.append(ISSUE_COMMENT_OR_MULTI)

    _validate_structure(tree, issues)
    _validate_projection(tree, issues)
    _validate_functions(tree, issues)

    alias_to_table = _validate_tables(tree, issues)
    _validate_columns(tree, alias_to_table, issues)
    _validate_domains(tree, issues)
    _validate_limit(tree, issues)

    return _result(issues)


# --------------------------------------------------------------------------
# 第 2 道防线：只读事务执行
# --------------------------------------------------------------------------

# 顺序有硬性要求：PostgreSQL 规定 SET TRANSACTION 必须是事务里的第一条语句，
# 而且如果它前面已经跑过查询，PostgreSQL 只会发一个警告然后**静默忽略**只读设置
# ——那样第二道防线就形同虚设，所以这三条必须最先执行且顺序固定。
#
# 值来自本模块的常量，不是调用方输入，因此可以安全地内联（SET 是工具语句，
# 不支持绑定参数，只能用字面量）。
READ_ONLY_PREAMBLE: tuple[str, ...] = (
    "SET TRANSACTION READ ONLY",
    f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'",
    f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'",
)


@dataclass(frozen=True)
class SafeQueryResult:
    """结构化只读查询结果。columns 与 rows 里的键顺序一致。"""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


def _to_json_value(value: Any) -> Any:
    """把数据库值转成可 JSON 序列化的形式。

    Decimal 转 float 是刻意的：数据库里金额始终是精确的 Numeric，
    只在「出网关」这一步转成 JSON number，方便前端图表直接使用。
    """
    if value is None:
        return None
    # bool 必须排在 int 前面：Python 里 bool 是 int 的子类
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)

    # 未知类型不做 str() 兜底：那会把「没处理过的类型」伪装成转换成功，
    # 让问题在更远的下游才爆炸，排查成本高得多。这里直接受控失败。
    raise ResultSerializationError()


# 连接层故障的 SQLSTATE：08 类是连接异常，28 类是认证失败（密码/账号不对），
# 加上几个「服务端主动断开」的错误码。
_CONNECTION_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


def _is_connection_failure(exc: BaseException) -> bool:
    if isinstance(exc, (OperationalError, InterfaceError, OSError)):
        return True

    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if not isinstance(sqlstate, str) or len(sqlstate) < 2:
        return False
    return sqlstate.startswith(("08", "28")) or sqlstate in _CONNECTION_SQLSTATES


async def _execute_readonly(conn: AsyncConnection, sql: str) -> SafeQueryResult:
    """在一个只读事务里执行查询。调用方必须已经校验过 sql。"""
    transaction = await conn.begin()
    try:
        for statement in READ_ONLY_PREAMBLE:
            await conn.execute(text(statement))

        result = await conn.execute(text(sql))
        try:
            columns = [str(name) for name in result.keys()]
            # 多取一行用来判断「是否超限」：LIMIT 已经卡在 200，这里是兜底。
            # CursorResult 的 fetchmany / close 是同步方法（只有 stream() 才返回异步结果）。
            raw_rows = result.fetchmany(MAX_RESULT_ROWS + 1)
        finally:
            result.close()
    finally:
        try:
            # 只读事务没有需要保留的写入，回滚能确保连接干干净净地还给连接池
            await transaction.rollback()
        except Exception:  # noqa: BLE001
            # 连接已损坏时回滚会再抛一次；不能让它盖住上面真正的失败原因
            logger.warning("安全查询事务回滚失败，该连接将被丢弃。")

    if len(raw_rows) > MAX_RESULT_ROWS:
        raise ResultTooLargeError()

    rows = [{name: _to_json_value(value) for name, value in zip(columns, row)} for row in raw_rows]
    return SafeQueryResult(columns=columns, rows=rows, row_count=len(rows))


async def run_readonly_query(sql: str, *, engine: AsyncEngine | None = None) -> SafeQueryResult:
    """在只读事务中执行已通过校验的 SQL。

    复用项目现有的 AsyncEngine（app.repositories.database），不另外配置一套连接。
    """
    engine = engine if engine is not None else get_engine()

    # 区分「连都没连上」和「连上了但语句失败」：前者是下游不可用（503），
    # 后者是调用方查询本身的问题（400）。仅靠异常类型判断不准——
    # 连接被拒时 asyncpg 直接抛的是 ConnectionRefusedError，不是 SQLAlchemy 异常。
    connected = False
    try:
        async with engine.connect() as conn:
            connected = True
            return await _execute_readonly(conn, sql)
    except SafeQueryError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 只记录异常类名：数据库异常的原文里可能带着 SQL 片段和连接信息
        if not connected or _is_connection_failure(exc):
            logger.warning("安全查询无法连接数据库：%s", type(exc).__name__)
            raise DatabaseUnavailableError() from exc
        logger.warning("安全查询执行失败：%s", type(exc).__name__)
        raise QueryExecutionError() from exc


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------

# 查询执行器的签名。抽成参数是为了让测试可以注入替身，
# 从而在不连数据库的情况下验证「校验不通过时执行器一次都不会被调用」。
QueryRunner = Callable[[str], Awaitable[SafeQueryResult]]


async def execute_safe_query(sql: str, *, runner: QueryRunner | None = None) -> SafeQueryResult:
    """校验并执行一条只读分析 SQL。

    职责边界：本函数只做「校验 → 执行」，不涉及任何 HTTP 概念。
    """
    validation = validate_safe_select(sql)
    if not validation["passed"]:
        # 只记录固定文案，绝不把 SQL 原文写进日志
        logger.warning("安全查询被拒绝：%s", " ".join(validation["issues"]))
        raise UnsafeSqlError(validation["issues"])

    execute = runner if runner is not None else run_readonly_query
    return await execute(sql)
