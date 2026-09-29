"""受控 SQL 安全策略的纯函数测试。

validate_safe_select 不连数据库、不发网络请求，因此这里可以放心穷举各种绕过写法。
覆盖任务书点名的全部合法与非法用例，并额外断言：
- 每条 issue 都是模块里预定义的固定文案，不含调用方输入；
- 校验过程完全不碰数据库。
"""

import ast
import inspect

import pytest

import app.services.safe_query as safe_query
from app.services.safe_query import MAX_SQL_LENGTH, validate_safe_select

# 模块里所有固定文案。issues 只允许从这里取值。
FIXED_ISSUES = {value for name, value in vars(safe_query).items() if name.startswith("ISSUE_")}

# --------------------------------------------------------------------------
# 合法查询：任务书要求的四类分析场景
# --------------------------------------------------------------------------

TREND_SQL = """
SELECT date_dim.month,
       SUM(orders.net_amount) AS sales_amount
FROM orders
JOIN date_dim ON orders.date_id = date_dim.date_id
GROUP BY date_dim.month
ORDER BY date_dim.month
LIMIT 12
"""

PRODUCT_RANKING_SQL = """
SELECT products.product_name,
       products.category_name,
       SUM(orders.net_amount) AS sales_amount,
       SUM(orders.quantity) AS units_sold
FROM orders
JOIN products ON orders.product_id = products.product_id
GROUP BY products.product_name, products.category_name
ORDER BY sales_amount DESC
LIMIT 10
"""

REGION_SALES_SQL = """
SELECT regions.region_name,
       SUM(orders.net_amount) AS sales_amount,
       COUNT(*) AS order_count
FROM orders
JOIN regions ON orders.region_id = regions.region_id
GROUP BY regions.region_name
ORDER BY sales_amount DESC
LIMIT 10
"""

MEMBER_REPURCHASE_SQL = """
SELECT customers.member_level,
       COUNT(DISTINCT orders.customer_id) AS buyer_count,
       COUNT(*) AS order_count,
       AVG(orders.net_amount) AS avg_amount
FROM orders
JOIN customers ON orders.customer_id = customers.customer_id
GROUP BY customers.member_level
ORDER BY buyer_count DESC
LIMIT 10
"""

LEGAL_QUERIES = {
    "趋势查询": TREND_SQL,
    "商品排行": PRODUCT_RANKING_SQL,
    "区域拆分": REGION_SALES_SQL,
    "会员复购": MEMBER_REPURCHASE_SQL,
}


def assert_rejected(sql: str) -> list[str]:
    """断言被拒绝，并返回 issues 供进一步检查。"""
    result = validate_safe_select(sql)
    assert result["passed"] is False, f"本应拒绝却通过了：{sql}"
    assert result["issues"], "被拒绝但没有任何说明"
    return result["issues"]


def assert_issues_are_fixed_text(issues: list[str], sql: str) -> None:
    """issues 只能是预定义文案，绝不能回显调用方的 SQL。"""
    for issue in issues:
        assert issue in FIXED_ISSUES, f"出现了非预定义文案：{issue}"
        assert sql not in issue, "issue 里回显了原始 SQL"
        assert sql.strip() not in issue, "issue 里回显了原始 SQL"


# --------------------------------------------------------------------------
# 合法用例
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LEGAL_QUERIES))
def test_legal_analysis_queries_pass(name):
    result = validate_safe_select(LEGAL_QUERIES[name])
    assert result["passed"] is True, f"{name} 被误判：{result['issues']}"
    assert result["issues"] == []


def test_alias_reference_in_order_by_is_allowed():
    """ORDER BY 引用输出别名是标准写法，且别名只能指向已校验的投影，所以放行。"""
    sql = """
    SELECT products.category_name, SUM(orders.net_amount) AS sales_amount
    FROM orders
    JOIN products ON orders.product_id = products.product_id
    GROUP BY products.category_name
    ORDER BY sales_amount DESC
    LIMIT 20
    """
    assert validate_safe_select(sql)["passed"] is True


def test_table_alias_is_allowed():
    sql = """
    SELECT d.month, SUM(o.net_amount) AS sales_amount
    FROM orders AS o
    JOIN date_dim AS d ON o.date_id = d.date_id
    GROUP BY d.month
    ORDER BY d.month
    LIMIT 12
    """
    assert validate_safe_select(sql)["passed"] is True


@pytest.mark.parametrize("limit", [1, 100, 200])
def test_limit_boundaries_are_allowed(limit):
    sql = f"SELECT COUNT(*) AS c FROM orders LIMIT {limit}"
    assert validate_safe_select(sql)["passed"] is True


def test_count_star_is_allowed():
    """COUNT(*) 里的 * 是合法聚合，不能和 SELECT * 混为一谈。"""
    sql = "SELECT COUNT(*) AS total FROM orders LIMIT 1"
    assert validate_safe_select(sql)["passed"] is True


# --------------------------------------------------------------------------
# 非法用例：写操作
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE products SET unit_price = 0",
        "INSERT INTO orders (order_no) VALUES ('X')",
        "DROP TABLE customers",
        "ALTER TABLE orders ADD COLUMN x int",
        "CREATE TABLE t (id int)",
        "TRUNCATE TABLE orders",
        "GRANT ALL ON orders TO public",
        "REVOKE ALL ON orders FROM public",
        "COPY orders TO '/tmp/x.csv'",
        "EXPLAIN SELECT orders.order_id FROM orders LIMIT 5",
        "VACUUM orders",
    ],
)
def test_write_and_admin_statements_are_rejected(sql):
    assert_issues_are_fixed_text(assert_rejected(sql), sql)


# --------------------------------------------------------------------------
# 非法用例：注入形态
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT orders.order_id FROM orders LIMIT 5; DELETE FROM orders",
        "SELECT orders.order_id FROM orders LIMIT 5; SELECT orders.order_id FROM orders LIMIT 5",
    ],
)
def test_multiple_statements_are_rejected(sql):
    issues = assert_rejected(sql)
    assert any("多条语句" in issue or "单条 SELECT" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT orders.order_id FROM orders LIMIT 5 -- 注释",
        "SELECT orders.order_id -- 偷偷加的注释\nFROM orders LIMIT 5",
        "SELECT orders.order_id /* 块注释 */ FROM orders LIMIT 5",
    ],
)
def test_comments_are_rejected(sql):
    assert_issues_are_fixed_text(assert_rejected(sql), sql)


def test_trailing_semicolon_is_rejected():
    """分号一律拒绝，哪怕只是个尾分号——避免和「多语句」的边界含糊不清。"""
    assert_issues_are_fixed_text(
        assert_rejected("SELECT orders.order_id FROM orders LIMIT 5;"),
        "SELECT orders.order_id FROM orders LIMIT 5;",
    )


# --------------------------------------------------------------------------
# 非法用例：查询形态
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders LIMIT 5",
        "SELECT orders.* FROM orders LIMIT 5",
        "SELECT *, COUNT(*) AS c FROM orders LIMIT 5",
    ],
)
def test_select_star_is_rejected(sql):
    issues = assert_rejected(sql)
    assert any("SELECT *" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


def test_cte_is_rejected():
    sql = """
    WITH monthly AS (SELECT orders.date_id FROM orders)
    SELECT monthly.date_id FROM monthly LIMIT 5
    """
    issues = assert_rejected(sql)
    assert any("CTE" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s.date_id FROM (SELECT orders.date_id FROM orders) AS s LIMIT 5",
        "SELECT orders.order_id FROM orders WHERE orders.customer_id IN "
        "(SELECT customers.customer_id FROM customers) LIMIT 5",
        "SELECT orders.order_id FROM orders WHERE orders.quantity > "
        "(SELECT AVG(orders.quantity) FROM orders) LIMIT 5",
    ],
)
def test_subquery_is_rejected(sql):
    issues = assert_rejected(sql)
    assert any("子查询" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT orders.order_id FROM orders UNION SELECT orders.order_id FROM orders LIMIT 5",
        "SELECT orders.order_id FROM orders INTERSECT SELECT orders.order_id FROM orders LIMIT 5",
        "SELECT orders.order_id FROM orders EXCEPT SELECT orders.order_id FROM orders LIMIT 5",
    ],
)
def test_set_operations_are_rejected(sql):
    issues = assert_rejected(sql)
    assert any("UNION" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


def test_window_function_is_rejected():
    sql = (
        "SELECT SUM(orders.net_amount) OVER (PARTITION BY orders.region_id) AS s "
        "FROM orders LIMIT 5"
    )
    issues = assert_rejected(sql)
    assert any("窗口函数" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


# --------------------------------------------------------------------------
# 非法用例：LIMIT
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT orders.order_id FROM orders",
        "SELECT COUNT(*) AS c FROM orders",
        "SELECT orders.order_id FROM orders LIMIT 0",
        "SELECT orders.order_id FROM orders LIMIT 201",
        "SELECT orders.order_id FROM orders LIMIT 1000",
        "SELECT orders.order_id FROM orders LIMIT -1",
        "SELECT orders.order_id FROM orders LIMIT ALL",
        "SELECT orders.order_id FROM orders LIMIT 1+1",
    ],
)
def test_bad_limit_is_rejected(sql):
    issues = assert_rejected(sql)
    assert any("LIMIT" in issue for issue in issues)
    assert_issues_are_fixed_text(issues, sql)


def test_limit_is_not_auto_filled():
    """缺失 LIMIT 必须拒绝，而不是由服务端悄悄补一个。"""
    result = validate_safe_select("SELECT orders.order_id FROM orders")
    assert result["passed"] is False


# --------------------------------------------------------------------------
# 非法用例：表与字段
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["information_schema.tables", "pg_catalog.pg_tables", "pg_stat_activity", "alembic_version"],
)
def test_unauthorized_tables_are_rejected(table):
    sql = f"SELECT t.table_name FROM {table} AS t LIMIT 5"
    issues = assert_rejected(sql)
    assert safe_query.ISSUE_UNAUTHORIZED_TABLE in issues
    assert_issues_are_fixed_text(issues, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT orders.created_at FROM orders LIMIT 5",
        "SELECT customers.customer_name FROM customers LIMIT 5",
        "SELECT products.created_at FROM products LIMIT 5",
        "SELECT date_dim.is_weekend FROM date_dim LIMIT 5",
        "SELECT regions.region_level FROM regions LIMIT 5",
    ],
)
def test_unauthorized_columns_are_rejected(sql):
    issues = assert_rejected(sql)
    assert safe_query.ISSUE_UNAUTHORIZED_COLUMN in issues
    assert_issues_are_fixed_text(issues, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT month FROM orders LIMIT 5",
        "SELECT net_amount FROM orders LIMIT 5",
        "SELECT SUM(net_amount) AS s FROM orders LIMIT 5",
    ],
)
def test_unqualified_columns_are_rejected(sql):
    issues = assert_rejected(sql)
    assert safe_query.ISSUE_UNQUALIFIED_COLUMN in issues
    assert_issues_are_fixed_text(issues, sql)


def test_unqualified_column_in_order_by_is_still_rejected():
    """ORDER BY 里没写表名的真实列同样要拒绝——它可能指向未授权字段。"""
    sql = """
    SELECT orders.order_id, SUM(orders.net_amount) AS sales_amount
    FROM orders
    GROUP BY orders.order_id
    ORDER BY created_at DESC
    LIMIT 5
    """
    issues = assert_rejected(sql)
    assert safe_query.ISSUE_UNQUALIFIED_COLUMN in issues


# --------------------------------------------------------------------------
# 非法用例：函数
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "pg_sleep(10)",
        "current_setting('data_directory')",
        "set_config('x', 'y', false)",
        "version()",
        "pg_read_file('/etc/passwd')",
        "ROUND(AVG(orders.net_amount), 2)",
        "COALESCE(orders.net_amount, 0)",
        "LOWER(products.product_name)",
        "CAST(orders.quantity AS text)",
    ],
)
def test_unauthorized_functions_are_rejected(expression):
    sql = f"SELECT {expression} AS v FROM orders LIMIT 5"
    issues = assert_rejected(sql)
    assert safe_query.ISSUE_UNAUTHORIZED_FUNCTION in issues
    assert_issues_are_fixed_text(issues, sql)


def test_case_expression_is_rejected():
    """CASE 会被解析成 Case / If 两个函数节点，不在白名单内。"""
    sql = (
        "SELECT CASE WHEN orders.quantity > 1 THEN 1 ELSE 0 END AS flag "
        "FROM orders LIMIT 5"
    )
    issues = assert_rejected(sql)
    assert safe_query.ISSUE_UNAUTHORIZED_FUNCTION in issues


# --------------------------------------------------------------------------
# 非法用例：文本层面
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql", ["", "   ", "\n\t "])
def test_blank_sql_is_rejected(sql):
    result = validate_safe_select(sql)
    assert result["passed"] is False
    assert result["issues"] == [safe_query.ISSUE_EMPTY]


def test_overlong_sql_is_rejected():
    sql = "SELECT orders.order_id FROM orders LIMIT 5" + " " * MAX_SQL_LENGTH
    result = validate_safe_select(sql)
    assert result["passed"] is False
    assert safe_query.ISSUE_TOO_LONG in result["issues"]


def test_unparseable_sql_is_rejected():
    issues = assert_rejected("SELECT FROM WHERE GROUP")
    assert safe_query.ISSUE_PARSE in issues


def test_issues_never_contain_the_raw_sql():
    """把各种非法 SQL 过一遍，确认没有任何一条把原文回显进 issues。"""
    dangerous = [
        "DELETE FROM orders",
        "SELECT * FROM orders LIMIT 5",
        "SELECT pg_sleep(10) AS v FROM orders LIMIT 5",
        "SELECT t.table_name FROM information_schema.tables AS t LIMIT 5",
        "SELECT orders.order_id FROM orders",
        "WITH x AS (SELECT orders.order_id FROM orders) SELECT x.order_id FROM x LIMIT 5",
    ]
    for sql in dangerous:
        result = validate_safe_select(sql)
        joined = " ".join(result["issues"])
        assert sql not in joined
        assert "information_schema" not in joined
        assert "pg_sleep" not in joined


# --------------------------------------------------------------------------
# 架构约束与纯度
# --------------------------------------------------------------------------


def test_validation_does_not_touch_the_database(monkeypatch):
    """校验必须纯粹在内存里完成，绝不能因为校验而连库。"""

    def explode(*args, **kwargs):  # pragma: no cover - 只在被错误调用时触发
        raise AssertionError("校验阶段不应该访问数据库")

    monkeypatch.setattr(safe_query, "get_engine", explode)

    for sql in LEGAL_QUERIES.values():
        validate_safe_select(sql)
    assert_rejected("DELETE FROM orders")


def test_safe_query_does_not_depend_on_the_agent():
    """数据中台不能反向依赖 AI 中台。

    直接检查模块的 import 语句，而不是字符串搜索——模块文档里会提到
    app.agent / LangGraph 这些名字，用字符串搜会误报。
    """
    tree = ast.parse(inspect.getsource(safe_query))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_prefixes = ("app.agent", "langgraph", "langchain", "openai")
    offenders = [name for name in imported if name.startswith(forbidden_prefixes)]
    assert not offenders, f"安全查询服务引入了不该有的依赖：{offenders}"


def test_whitelist_matches_models():
    """白名单里的表与字段必须真实存在于 ORM 模型，避免登记了不存在的字段。"""
    from app.models import Base

    for table_name, columns in safe_query.ALLOWED_COLUMNS.items():
        assert table_name in Base.metadata.tables, f"白名单里的表不存在：{table_name}"
        model_columns = {column.name for column in Base.metadata.tables[table_name].columns}
        unknown = columns - model_columns
        assert not unknown, f"{table_name} 登记了不存在的字段：{sorted(unknown)}"


def test_only_whitelisted_tables_are_reachable():
    """默认拒绝：白名单里只有登记过的零售运营表 + 天猫 Gold 表。

    这张清单是**权限边界**的落地，不是配置细节。任何一张表加进来，
    都意味着 LLM 生成的 SQL 可以查它。加表必须是一次有意识的决定，
    所以这里用「相等」而不是「包含」。
    """
    assert set(safe_query.ALLOWED_COLUMNS) == {
        # 零售领域
        "customers",
        "products",
        "regions",
        "date_dim",
        "orders",
        "channels",
        "promotions",
        "order_operations",
        "inventory_snapshots",
        "ad_campaigns",
        "ad_daily_metrics",
        "after_sales",
        # 天猫领域：只有 Gold 汇总表
        "tmall_daily_metrics",
        "tmall_merchant_metrics",
        "tmall_category_metrics",
        "tmall_user_metrics",
        "tmall_funnel_metrics",
        "tmall_repurchase_metrics",
    }


@pytest.mark.parametrize(
    "table",
    [
        "tmall_user_events",
        "tmall_users",
        "tmall_repurchase_samples",
        "tmall_ingestion_runs",
    ],
)
def test_tmall_detail_tables_are_not_reachable(table):
    """天猫明细表一张都不能进白名单。

    这是整条权限设计的核心：只要 tmall_user_events 可查，
    任何 LLM 生成的 SQL 就能把**某一个用户的完整行为轨迹**拉出来。
    把它留在白名单之外，「模型能不能看到用户级明细」这个问题
    在表这一层就有答案，不依赖提示词、也不依赖模型的自觉。
    """
    assert table not in safe_query.ALLOWED_COLUMNS

    result = safe_query.validate_safe_select(
        f"SELECT {table}.event_count FROM {table} LIMIT 10"
    )
    assert not result["passed"]
    assert safe_query.ISSUE_UNAUTHORIZED_TABLE in result["issues"]


def test_tmall_gold_tables_are_queryable():
    """Gold 表必须真的能查——挡住明细不等于把整个天猫领域也挡掉了。"""
    result = safe_query.validate_safe_select(
        "SELECT tmall_daily_metrics.metric_date,"
        "       SUM(tmall_daily_metrics.event_count)"
        " FROM tmall_daily_metrics"
        " GROUP BY tmall_daily_metrics.metric_date"
        " LIMIT 50"
    )
    assert result["passed"], result["issues"]


def test_cross_domain_join_is_rejected():
    """`orders JOIN tmall_user_metrics` 两张表都在白名单里，语法完全合法。

    这个查询算出来的数字把 2014 年的行为记录和 2025 年的订单金额连在一起，
    没有业务含义，而且**不报任何错**。必须由跨领域规则显式拒绝。
    """
    result = safe_query.validate_safe_select(
        "SELECT orders.order_no, tmall_user_metrics.event_count"
        " FROM orders"
        " JOIN tmall_user_metrics ON tmall_user_metrics.user_id = orders.customer_id"
        " LIMIT 10"
    )
    assert not result["passed"]
    assert safe_query.ISSUE_CROSS_DOMAIN in result["issues"]


def test_cross_domain_check_does_not_fire_within_one_domain():
    retail = safe_query.validate_safe_select(
        "SELECT orders.order_no FROM orders"
        " JOIN customers ON customers.customer_id = orders.customer_id"
        " LIMIT 10"
    )
    assert safe_query.ISSUE_CROSS_DOMAIN not in retail["issues"]

    tmall = safe_query.validate_safe_select(
        "SELECT tmall_daily_metrics.metric_date"
        " FROM tmall_daily_metrics"
        " JOIN tmall_funnel_metrics ON tmall_funnel_metrics.action_type ="
        " tmall_daily_metrics.action_type"
        " LIMIT 10"
    )
    assert safe_query.ISSUE_CROSS_DOMAIN not in tmall["issues"]


def test_cross_domain_check_is_silent_for_unknown_tables():
    """未登记的表已经因为「未授权」被拒，再报一条跨领域只会让 issues 变长。"""
    result = safe_query.validate_safe_select(
        "SELECT orders.order_no, some_other_table.x FROM orders"
        " JOIN some_other_table ON some_other_table.x = orders.order_id LIMIT 10"
    )
    assert safe_query.ISSUE_UNAUTHORIZED_TABLE in result["issues"]
    assert safe_query.ISSUE_CROSS_DOMAIN not in result["issues"]
