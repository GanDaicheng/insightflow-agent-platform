"""SQL 草稿的安全校验：用 sqlglot 解析成 AST，再逐条检查。

为什么必须用 AST，不能用正则/字符串匹配？
字符串匹配是在「猜」SQL 的含义，而 SQL 有太多等价写法。举几个例子：

- `SELECT * FROM orders` 和 `SELECT  orders.*  FROM orders`（多个空格）和
  `SELECT/*x*/*FROM orders`，正则要么漏掉、要么被空白和注释绕过去。
- `-- 注释\nDELETE FROM orders` 里 DELETE 出现在注释后面，字符串一扫就命中，
  但它根本不是可执行语句；反过来 `SELECT 1;DELETE FROM orders` 分隔符千变万化。
- 表名大小写、加引号（`"orders"`）、加 schema 前缀（`public.orders`），
  字符串比较全部要单独处理。

AST 则不同：sqlglot 已经按 PostgreSQL 语法把语句解析成了结构化节点，
`SELECT 1;DELETE FROM orders` 就是两个节点，`"orders"` 和 `orders` 是同一个
表名节点。我们检查的是「语法结构」而不是「文本长相」，绕不过去。

边界说明：本模块只做**第一层**校验——语法层面「这条 SQL 长什么样」。
它不判断「这条 SQL 查出来的数据该不该给这个用户看」，也不真正执行任何东西。
权限、行级过滤、资源限制属于后续 run_safe_query 的职责。
"""

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.agent.data_query.catalog import DATASETS
from app.agent.data_query.constants import MAX_SQL_LIMIT
from app.agent.data_query.state import MatchedAsset, SqlValidation
from app.services.data_domains import cross_domain_violation, describe_domains

# 固定按 PostgreSQL 方言解析。用别的方言解析会改变语法规则
# （比如 MySQL 的反引号和 LIMIT x,y），校验结论就不可信了。
DIALECT = "postgres"

# sqlglot 的节点类名 → 业务上认识的语句名。
# 键统一小写，避免依赖 sqlglot 内部的大小写习惯。
_DANGEROUS_NODE_NAMES = {
    "insert": "INSERT",
    "update": "UPDATE",
    "delete": "DELETE",
    "drop": "DROP",
    "alter": "ALTER",
    "create": "CREATE",
    "truncatetable": "TRUNCATE",
    "grant": "GRANT",
    "revoke": "REVOKE",
    "copy": "COPY",
}

# Keep the Agent-side guard aligned with services.safe_query. Arithmetic and
# boolean operators are expressions, not callable SQL functions.
_ALLOWED_FUNCTIONS: tuple[type[exp.Func], ...] = (
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
)


def _statement_label(node: exp.Expression) -> str | None:
    """返回节点的「危险语句名」；安全则返回 None。

    CALL / EXECUTE / VACUUM 这类 sqlglot 没有专门节点，会落到 exp.Command，
    真正的动词名在 args["this"] 里（字符串）。一律按危险处理——
    「解析不出来是什么」的语句，安全策略上应当拒绝而不是放行。
    """
    if isinstance(node, exp.Command):
        return str(node.args.get("this") or "COMMAND").upper()
    return _DANGEROUS_NODE_NAMES.get(type(node).__name__.lower())


def _matched_dataset_fields(
    matched_assets: list[MatchedAsset] | None,
) -> dict[str, set[str]]:
    """已匹配数据集 → 它登记的字段集合。

    字段口径以 catalog.py 为唯一来源：即使问题匹配到了 orders，
    也只能用 catalog 里给 orders 登记过的那些字段。
    """
    matched_names = {
        asset.get("name")
        for asset in (matched_assets or [])
        if asset.get("kind") == "dataset"
    }
    return {
        dataset["name"]: set(dataset["fields"])
        for dataset in DATASETS
        if dataset["name"] in matched_names
    }


def _dedupe(items: list[str]) -> list[str]:
    """按出现顺序去重。同一个问题只报一次，读起来才不吵。"""
    return list(dict.fromkeys(items))


def _result(issues: list[str]) -> SqlValidation:
    issues = _dedupe(issues)
    return {"passed": not issues, "issues": issues}


def _validate_functions(tree: exp.Expression, issues: list[str]) -> None:
    for node in tree.walk():
        if isinstance(node, exp.Binary) and not isinstance(node, (exp.Case, exp.If)):
            continue
        if not isinstance(node, exp.Func):
            continue
        if isinstance(node, (exp.Case, exp.If)):
            ancestor = node.parent
            while ancestor is not None and not isinstance(ancestor, _ALLOWED_FUNCTIONS):
                ancestor = ancestor.parent
            if isinstance(ancestor, _ALLOWED_FUNCTIONS):
                continue
        if not isinstance(node, _ALLOWED_FUNCTIONS):
            issues.append("查询使用了不允许的 SQL 函数。")
            return


def validate_sql_draft(
    sql: str, matched_assets: list[MatchedAsset] | None
) -> SqlValidation:
    """校验 SQL 草稿，返回 {passed, issues}。

    关键约定：**全量收集问题，不在第一个问题上就返回**。
    下游的 repair_sql 只有一次修复机会，把问题一次给全，它才有可能一次改对；
    只报第一条的话，修完第一条还会卡在第二条上，白白浪费掉那次机会。
    """
    issues: list[str] = []

    # 1. 非空
    if not sql or not sql.strip():
        return _result(["SQL 草稿为空。"])

    # 2. 解析。解析不了就没法做任何结构化检查，只能在这里收口。
    try:
        statements = [stmt for stmt in sqlglot.parse(sql, dialect=DIALECT) if stmt]
    except ParseError:
        return _result(["SQL 无法解析为合法的 PostgreSQL 语句。"])

    if not statements:
        return _result(["SQL 未包含任何可解析的语句。"])

    tree = statements[0]

    # 3. 注释。sqlglot 把注释挂到具体节点上，所以要遍历整棵树找。
    if any(node.comments for node in tree.walk()):
        issues.append("禁止在 SQL 中使用注释。")

    # 4. 单语句。多语句是最典型的注入入口：前半段看着正常，后半段才是目的。
    if len(statements) != 1:
        issues.append(f"只允许单条 SQL 语句，当前包含 {len(statements)} 条。")

    # 5. 危险语句。对所有语句检查，这样 "SELECT 1; DELETE FROM orders"
    #    会同时报出「多语句」和「禁止使用 DELETE」，而不是只报一个。
    for stmt in statements:
        label = _statement_label(stmt)
        if label:
            issues.append(f"禁止使用 {label}。")

    # 6. 顶层必须是 SELECT。非 SELECT 就没法继续做表/字段检查了，到此为止。
    if not isinstance(tree, exp.Select):
        if _statement_label(tree) is None:
            issues.append(
                f"只允许 SELECT 查询，当前是 {type(tree).__name__.upper()}。"
            )
        return _result(issues)

    # 7. 禁止 WITH / CTE。
    #    注意：不能只看 tree.args["with"]，sqlglot 在某些写法下不填这个字段，
    #    但 exp.With / exp.CTE 节点一定在树里，遍历节点更可靠。
    if any(isinstance(node, (exp.With, exp.CTE)) for node in tree.walk()):
        issues.append("禁止使用 WITH / CTE。")

    # 8. 禁止子查询。exp.Subquery 覆盖 FROM (SELECT ...)、IN (SELECT ...) 等写法。
    if any(isinstance(node, exp.Subquery) for node in tree.walk()):
        issues.append("禁止使用子查询。")

    # 9. 兜底的嵌套危险节点检查。正常情况下 SELECT 里嵌不了 DML，
    #    但这是「拒绝危险节点」这条规则的直接落地，成本很低。
    for node in tree.walk():
        label = _statement_label(node)
        if label:
            issues.append(f"禁止使用 {label}。")

    # 10. 禁止 SELECT *
    for projection in tree.expressions:
        # 只认投影列表里裸的 *：
        #   SELECT *        → exp.Star
        #   SELECT orders.* → exp.Column，名字是 "*"
        # COUNT(*) 是 exp.Count，不在这个判断范围里——它是合法的聚合，不是 SELECT *。
        if isinstance(projection, exp.Star):
            issues.append("禁止使用 SELECT *，请显式列出需要的字段。")
            break
        if isinstance(projection, exp.Column) and projection.name == "*":
            issues.append("禁止使用 SELECT *，请显式列出需要的字段。")
            break

    _validate_functions(tree, issues)

    # 11. 表必须来自已匹配的数据集
    allowed_fields = _matched_dataset_fields(matched_assets)
    alias_to_dataset: dict[str, str] = {}

    # 先做跨领域检查，再逐表检查授权。
    # 顺序很重要：`orders JOIN tmall_user_metrics` 里两张表**都在**目录里，
    # 授权检查会全部放行，SQL 语法也完全合法——它算出来的数字把 2014 年的
    # 行为记录和 2025 年的订单金额连在一起，没有业务含义，而且不会报任何错。
    # 只有这条规则能把它拦下来，所以它必须先跑，且报的是「跨领域」而不是
    # 某个表未授权——后者会把修复方向指到完全错误的地方。
    referenced = {table.name.lower() for table in tree.find_all(exp.Table)}
    violation = cross_domain_violation(referenced)
    if violation is not None:
        issues.append(
            f"禁止跨领域关联查询：{describe_domains(violation)}"
            f" 的时间范围与业务口径互不通用。"
        )

    for table in tree.find_all(exp.Table):
        if table.db or table.catalog:
            issues.append(f"不允许使用库名或 schema 限定：{table.sql(dialect=DIALECT)}。")
        if table.name not in allowed_fields:
            issues.append(f"引用了未匹配的数据集：{table.name}。")
            continue
        if table.alias:
            alias_to_dataset[table.alias] = table.name

    # 12. 字段必须在对应数据集登记的字段内，且必须写完整表名
    #
    # LIMIT 子句里的东西不算「字段引用」：`LIMIT ALL` 会被 sqlglot 解析成
    # 一个叫 ALL 的列，如果不排除，issues 里就会冒出一条
    # 「字段必须写完整表名：ALL」——它会把 repair_sql 引到完全错误的方向。
    limit_node = tree.args.get("limit")
    limit_columns = (
        {id(column) for column in limit_node.find_all(exp.Column)}
        if limit_node is not None
        else set()
    )

    for column in tree.find_all(exp.Column):
        if column.name == "*":
            continue  # orders.* 已在第 10 步处理
        if id(column) in limit_columns:
            continue  # LIMIT 里的标识符由第 13 步单独判断

        if not column.table:
            issues.append(
                f"字段必须写完整表名，例如 orders.net_amount：{column.name}。"
            )
            continue

        dataset = alias_to_dataset.get(column.table, column.table)
        if dataset not in allowed_fields:
            continue  # 表本身已经报过错了，不重复刷屏

        if column.name not in allowed_fields[dataset]:
            issues.append(f"数据集 {dataset} 中不存在字段：{column.name}。")

    # 13. LIMIT
    if limit_node is None:
        # sqlglot 30 no longer preserves PostgreSQL's `LIMIT ALL` as a Limit
        # node. Keep the public validation contract stable across parser versions.
        if re.search(r"\blimit\s+all\b", sql, flags=re.IGNORECASE):
            issues.append("LIMIT 必须是正整数。")
        else:
            issues.append(f"查询必须包含不超过 {MAX_SQL_LIMIT} 的 LIMIT。")
    else:
        value = limit_node.expression
        if not isinstance(value, exp.Literal) or not value.is_int:
            # 覆盖 LIMIT ALL、LIMIT 1+1、LIMIT ? 这些非字面量的写法
            issues.append("LIMIT 必须是正整数。")
        else:
            amount = int(value.this)
            if amount < 1:
                issues.append("LIMIT 必须是正整数。")
            elif amount > MAX_SQL_LIMIT:
                issues.append(
                    f"LIMIT 不能超过 {MAX_SQL_LIMIT}，当前是 {amount}。"
                )

    return _result(issues)
