"""天猫 Gold 层口径与校验的测试。

分三类：
1. 校验函数的纯逻辑（给定输入，判定对不对）；
2. Gold 刷新 SQL 的**静态**正确性——列名必须真实存在于模型里，
   且只碰 tmall 自己的表；
3. 预期规模登记表的自洽性。
"""

from decimal import Decimal

import pytest
import sqlglot
from sqlglot import exp

from app.models import Base
from app.models.tmall import GOLD_TABLES, SILVER_TABLES
from app.services import tmall_analytics as analytics
from app.services.tmall_analytics import (
    BASELINE_SOURCES,
    EXPECTED_COUNTS,
    EXPECTED_DATE_MAX,
    EXPECTED_DATE_MIN,
    GOLD_CONSISTENCY_SQL,
    GOLD_REFRESH_STATEMENTS,
    SILVER_DELETE_STATEMENTS,
    check_action_distribution,
    check_date_range,
    check_gold_consistency,
    check_idempotency,
    check_no_orphans,
    check_row_counts,
    check_sampling_consistency,
    compare_to_baseline,
    expected_counts_for,
    sampling_violation_sql,
)

TMALL_TABLE_NAMES = {name for name, _ in GOLD_TABLES} | {
    name for name, _ in SILVER_TABLES
} | {"tmall_ingestion_runs"}

# 全部 tmall 表的列名并集。用来判定「SQL 里出现的列名是不是真的存在」。
KNOWN_COLUMNS = {
    column.name
    for table in Base.metadata.tables.values()
    if table.name.startswith("tmall")
    for column in table.columns
}


def _cte_output_names(tree: exp.Expression) -> set[str]:
    """CTE 的投影别名不算「表里的列」，它们是中间结果的名字。"""
    names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        for projection in cte.this.expressions:
            if projection.alias:
                names.add(projection.alias.lower())
    return names


def _cte_names(tree: exp.Expression) -> set[str]:
    """CTE 自己的名字。sqlglot 把 `FROM per_action` 里的 per_action 也解析成
    exp.Table，所以「引用了哪些表」这个问题必须先减掉 CTE 名才准。"""
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _referenced_tables(sql: str) -> set[str]:
    return {table.name.lower() for table in sqlglot.parse_one(sql, dialect="postgres").find_all(exp.Table)}


# --------------------------------------------------------------------------
# 预期规模登记表
# --------------------------------------------------------------------------


def test_baseline_is_registered_for_the_documented_sampling_params():
    counts = expected_counts_for(55, 0)
    assert counts is not None
    assert counts["tmall_users"] == 7712
    assert counts["tmall_user_events"] == 998542
    assert counts["tmall_repurchase_samples_train"] == 4700
    assert counts["tmall_repurchase_samples_test"] == 4858


def test_action_counts_sum_to_the_event_total():
    """四个动作的事件数必须恰好等于事件总数。

    这条自洽性检查能发现登记表被改坏（比如只更新了总数忘了更新某个动作）。
    """
    counts = EXPECTED_COUNTS[(55, 0)]
    assert (
        counts["click"] + counts["cart"] + counts["buy"] + counts["favorite"]
        == counts["tmall_user_events"]
    )


def test_unregistered_sampling_params_return_none():
    """换一组参数是没有基准线的，不是错误。"""
    assert expected_counts_for(7, 3) is None


def test_registered_baseline_contains_no_monetary_metric():
    """这份数据集没有价格，任何金额类指标都不该出现在登记表里。"""
    forbidden = ("gmv", "amount", "revenue", "sales", "profit", "aov", "order")
    for key in EXPECTED_COUNTS[(55, 0)]:
        lowered = key.lower()
        for token in forbidden:
            assert token not in lowered, f"登记表里出现了金额/订单类指标：{key}"


def test_expected_date_range_is_registered_as_strings():
    assert (EXPECTED_DATE_MIN, EXPECTED_DATE_MAX) == ("2014-05-11", "2014-11-12")


# --------------------------------------------------------------------------
# compare_to_baseline
# --------------------------------------------------------------------------


BASELINE_SAMPLED = {"user_info": 7712, "user_log": 998542, "train": 4700, "test": 4858}
BASELINE_ACTIONS = {"click": 881857, "cart": 1343, "buy": 60036, "favorite": 55306}


def test_baseline_comparison_matches_the_registered_numbers():
    comparisons = compare_to_baseline(
        sampled_row_counts=BASELINE_SAMPLED, action_counts=BASELINE_ACTIONS, modulus=55, residue=0
    )
    assert comparisons is not None
    assert all(item.matches for item in comparisons)
    assert {item.label for item in comparisons} == set(EXPECTED_COUNTS[(55, 0)])


def test_baseline_comparison_flags_a_mismatch():
    """映射写错时症状是「校验报告说一切正常」——最不该靠人眼发现的一类错误，
    所以「对照逻辑本身对不对」必须有测试。"""
    comparisons = compare_to_baseline(
        sampled_row_counts={**BASELINE_SAMPLED, "user_log": 998541},
        action_counts=BASELINE_ACTIONS,
        modulus=55,
        residue=0,
    )
    assert comparisons is not None
    offenders = [item for item in comparisons if not item.matches]
    assert [item.label for item in offenders] == ["tmall_user_events"]
    assert offenders[0].actual == 998541


def test_baseline_comparison_reads_actions_from_the_action_counter():
    """动作数只能从动作计数器取，不能从文件行数取。

    取错源的话，「事件总数对得上」会让四个动作的偏差全部隐藏起来。
    """
    comparisons = compare_to_baseline(
        sampled_row_counts=BASELINE_SAMPLED,
        action_counts={**BASELINE_ACTIONS, "cart": 0},
        modulus=55,
        residue=0,
    )
    assert comparisons is not None
    cart = next(item for item in comparisons if item.label == "cart")
    assert cart.actual == 0 and not cart.matches


def test_baseline_comparison_returns_none_for_unregistered_params():
    assert (
        compare_to_baseline(
            sampled_row_counts={}, action_counts={}, modulus=7, residue=1
        )
        is None
    )


def test_baseline_sources_cover_every_registered_key():
    """登记表里出现了 BASELINE_SOURCES 没有登记的键，就必须在这里发现。

    否则那一项会被静默跳过——校验报告少打印一行，没人会注意到。
    """
    assert set(BASELINE_SOURCES) == set(EXPECTED_COUNTS[(55, 0)])


# --------------------------------------------------------------------------
# check_row_counts
# --------------------------------------------------------------------------


def test_row_counts_pass_when_everything_matches():
    expected = {"a": 1, "b": 2}
    result = check_row_counts({"a": 1, "b": 2}, expected)
    assert result.passed


def test_row_counts_report_every_mismatch_not_just_the_first():
    """一次报全部，避免「改一个跑一次」的循环。"""
    result = check_row_counts({"a": 9, "b": 8}, {"a": 1, "b": 2})
    assert not result.passed
    assert "a" in result.summary and "b" in result.summary


def test_row_counts_skip_rather_than_fail_for_unregistered_params():
    """没有基准线时报告「跳过」，不算失败。

    判为失败会让 --sample-modulus 变成一个只能填 55 的隐式约束，
    而抽样参数本来就该由调用方自由选择。
    """
    result = check_row_counts({"a": 1}, None)
    assert result.passed
    assert "跳过" in result.summary


# --------------------------------------------------------------------------
# check_action_distribution
# --------------------------------------------------------------------------


ROWS = [
    {"action_type": "click", "event_count": 881857},
    {"action_type": "cart", "event_count": 1343},
    {"action_type": "buy", "event_count": 60036},
    {"action_type": "favorite", "event_count": 55306},
]


def test_action_distribution_passes_for_the_full_set():
    assert check_action_distribution(ROWS, None).passed


def test_action_distribution_fails_when_an_action_is_missing():
    result = check_action_distribution(ROWS[:2], None)
    assert not result.passed
    assert "favorite" in result.summary or "buy" in result.summary


def test_action_distribution_compares_against_the_baseline_when_known():
    expected = {"click": 5, "cart": 1, "buy": 1, "favorite": 1}
    result = check_action_distribution(ROWS, expected)
    assert not result.passed
    assert "click" in result.summary


def test_action_distribution_evidence_includes_the_total():
    result = check_action_distribution(ROWS, None)
    assert any("合计" in line for line in result.evidence)


# --------------------------------------------------------------------------
# check_date_range
# --------------------------------------------------------------------------


def test_date_range_passes_within_the_registered_window():
    result = check_date_range(
        "2014-05-11", "2014-11-12", expected_min=EXPECTED_DATE_MIN, expected_max=EXPECTED_DATE_MAX
    )
    assert result.passed


def test_date_range_passes_for_a_narrower_sampled_window():
    """换个抽样参数抽到的端点本来就会变，不该判失败。"""
    result = check_date_range(
        "2014-06-01", "2014-10-01", expected_min=EXPECTED_DATE_MIN, expected_max=EXPECTED_DATE_MAX
    )
    assert result.passed


def test_date_range_fails_when_the_year_is_wrong():
    """年份补错（比如补成 2015）必须被抓到——MMDD 里没有年份信息，
    这是最容易被悄悄搞错的一项。"""
    result = check_date_range(
        "2015-05-11", "2015-11-12", expected_min=EXPECTED_DATE_MIN, expected_max=EXPECTED_DATE_MAX
    )
    assert not result.passed


def test_date_range_fails_when_month_and_day_are_swapped():
    result = check_date_range(
        "2014-11-05", "2014-12-11", expected_min=EXPECTED_DATE_MIN, expected_max=EXPECTED_DATE_MAX
    )
    assert not result.passed


def test_date_range_fails_on_an_empty_table():
    result = check_date_range(None, None, expected_min=EXPECTED_DATE_MIN, expected_max=EXPECTED_DATE_MAX)
    assert not result.passed


# --------------------------------------------------------------------------
# check_no_orphans
# --------------------------------------------------------------------------


def test_no_orphans_passes_when_every_reference_resolves():
    assert check_no_orphans({"event_users": 0, "repurchase_users": 0}).passed


def test_no_orphans_reports_which_relation_broke():
    result = check_no_orphans({"event_users": 3, "repurchase_users": 0})
    assert not result.passed
    assert "event_users" in result.summary


# --------------------------------------------------------------------------
# check_gold_consistency
# --------------------------------------------------------------------------


def test_gold_consistency_passes_when_totals_match():
    assert check_gold_consistency([("daily", 100, 100), ("funnel", 100, 100)]).passed


def test_gold_consistency_fails_and_names_the_offending_metric():
    result = check_gold_consistency([("daily", 99, 100)])
    assert not result.passed
    assert "daily" in result.summary


def test_gold_consistency_compares_as_integers():
    """asyncpg 会把 COUNT 返回成 int，但 SELECT COALESCE(SUM(...), 0) 在某些
    路径下会回来 Decimal。用 != 直接比会在 100 == Decimal('100') 上出错。"""
    assert check_gold_consistency([("daily", Decimal(100), 100)]).passed


# --------------------------------------------------------------------------
# check_idempotency
# --------------------------------------------------------------------------


def test_idempotency_passes_when_counts_are_unchanged():
    counts = {"tmall_users": 7712, "tmall_user_events": 998542}
    assert check_idempotency(counts, dict(counts)).passed


def test_idempotency_fails_when_a_rerun_adds_rows():
    result = check_idempotency(
        {"tmall_users": 7712, "tmall_user_events": 998542},
        {"tmall_users": 7712, "tmall_user_events": 999000},
    )
    assert not result.passed
    assert "tmall_user_events" in result.summary


def test_idempotency_spots_a_table_that_appeared_on_the_second_run():
    result = check_idempotency({"tmall_users": 0}, {"tmall_users": 0, "tmall_user_events": 5})
    assert not result.passed


# --------------------------------------------------------------------------
# check_sampling_consistency
# --------------------------------------------------------------------------


def test_sampling_consistency_passes_when_no_row_violates_the_modulus():
    result = check_sampling_consistency(
        {"tmall_users": 0, "tmall_user_events": 0}, 55, 0
    )
    assert result.passed
    assert "user_id % 55 == 0" in result.summary


def test_sampling_consistency_fails_and_names_the_table():
    result = check_sampling_consistency(
        {"tmall_users": 0, "tmall_user_events": 4}, 55, 0
    )
    assert not result.passed
    assert "tmall_user_events" in result.summary


# --------------------------------------------------------------------------
# sampling_violation_sql：受控拼接
# --------------------------------------------------------------------------


def test_sampling_violation_sql_targets_the_requested_table():
    sql = sampling_violation_sql("tmall_user_events", "user_id", 55, 0)
    assert sql == "SELECT COUNT(*) FROM tmall_user_events WHERE user_id % 55 <> 0"


@pytest.mark.parametrize("table", ["orders", "tmall_users; DROP TABLE orders", "pg_catalog.pg_user"])
def test_sampling_violation_sql_rejects_unknown_tables(table):
    """表名来自内部登记清单，不接受任意输入。

    这条断言防的是「将来有人图省事，把命令行参数直接传进来」——
    到那时这里会先炸，而不是把一段拼接的 SQL 发到数据库。
    """
    with pytest.raises(ValueError):
        sampling_violation_sql(table, "user_id", 55, 0)


def test_sampling_violation_sql_rejects_unknown_columns():
    with pytest.raises(ValueError):
        sampling_violation_sql("tmall_users", "merchant_id", 55, 0)


@pytest.mark.parametrize(("modulus", "residue"), [(0, 0), (55, 55), (55, -1)])
def test_sampling_violation_sql_rejects_invalid_params(modulus, residue):
    with pytest.raises(ValueError):
        sampling_violation_sql("tmall_users", "user_id", modulus, residue)


def test_sampling_violation_sql_rejects_non_integers():
    with pytest.raises(TypeError):
        sampling_violation_sql("tmall_users", "user_id", "55", 0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Gold SQL 的静态正确性（不需要数据库）
# --------------------------------------------------------------------------


def test_every_gold_table_has_a_refresh_statement():
    assert {name for name, _, _ in GOLD_REFRESH_STATEMENTS} == {name for name, _ in GOLD_TABLES}


def test_gold_refresh_deletes_before_inserting():
    """先清空再写入。顺序写反的话，每次导入都会在旧数据上追加一份。

    写法允许 `INSERT INTO ... SELECT` 与 `WITH ... INSERT INTO ... SELECT`
    两种。后者是复购口径需要的——它必须先把购买行为按「用户 × 商家」
    聚合一层，才能数出「买过不止一次」的用户。
    """
    for name, delete_sql, insert_sql in GOLD_REFRESH_STATEMENTS:
        assert delete_sql.strip().upper() == f"DELETE FROM {name}".upper()

        tree = sqlglot.parse_one(insert_sql, dialect="postgres")
        assert isinstance(tree, exp.Insert), f"{name} 的刷新语句不是 INSERT"
        assert tree.this.this.name.lower() == name, f"{name} 的刷新语句写到了别的表"


@pytest.mark.parametrize(
    ("name", "delete_sql", "insert_sql"),
    GOLD_REFRESH_STATEMENTS,
    ids=[name for name, _, _ in GOLD_REFRESH_STATEMENTS],
)
def test_gold_sql_references_only_real_tmall_columns(name, delete_sql, insert_sql):
    """SQL 里出现的每个列名都必须真实存在于 tmall 模型里。

    这类错误的默认表现是**运行时才炸**：`buy_counts` 这种拼写错误要等到
    刷新 Gold 的那一刻才会以 ProgrammingError 出现，而那一步在整条导入
    流水线的末端——前面几分钟的读取和 COPY 全都白做了。
    用 sqlglot 静态解析一遍，几毫秒就能把它拦下来。
    """
    tree = sqlglot.parse_one(insert_sql, dialect="postgres")
    referenced = {column.name.lower() for column in tree.find_all(exp.Column)}
    unknown = referenced - KNOWN_COLUMNS - _cte_output_names(tree)
    assert not unknown, f"{name} 引用了不存在的列：{sorted(unknown)}"


@pytest.mark.parametrize(
    ("name", "delete_sql", "insert_sql"),
    GOLD_REFRESH_STATEMENTS,
    ids=[name for name, _, _ in GOLD_REFRESH_STATEMENTS],
)
def test_gold_sql_only_touches_tmall_tables(name, delete_sql, insert_sql):
    """Gold 刷新只允许碰 tmall 自己的表。

    跨领域 JOIN（比如把天猫事件和零售订单连起来）会产出一个物理上能算、
    业务上毫无意义的数字，而且不会有任何报错。
    """
    tree = sqlglot.parse_one(insert_sql, dialect="postgres")
    tables = _referenced_tables(insert_sql) - _cte_names(tree)
    assert tables <= TMALL_TABLE_NAMES, f"{name} 引用了非天猫表：{sorted(tables - TMALL_TABLE_NAMES)}"


@pytest.mark.parametrize(
    ("name", "delete_sql", "insert_sql"),
    GOLD_REFRESH_STATEMENTS,
    ids=[name for name, _, _ in GOLD_REFRESH_STATEMENTS],
)
def test_gold_sql_inserts_the_same_number_of_columns_as_it_selects(name, delete_sql, insert_sql):
    """INSERT 的列清单与 SELECT 的投影数量必须相等。

    数量对不上 PostgreSQL 会报错，但那是在执行时；这里提前静态检查，
    改 SQL 时立刻就能发现，不用连数据库。
    """
    tree = sqlglot.parse_one(insert_sql, dialect="postgres")
    target_columns = [column.name for column in tree.this.expressions]
    projections = tree.expression.expressions
    assert len(target_columns) == len(projections), (
        f"{name}: INSERT 声明 {len(target_columns)} 列，SELECT 产出 {len(projections)} 列"
    )


def test_gold_sql_never_contains_a_monetary_expression():
    """这份数据没有价格，出现任何金额运算都说明口径被凭空发明了。"""
    forbidden = ("amount", "price", "revenue", "gmv", "profit", "quantity", "net_sales")
    for name, _, insert_sql in GOLD_REFRESH_STATEMENTS:
        lowered = insert_sql.lower()
        for token in forbidden:
            assert token not in lowered, f"{name} 的刷新 SQL 里出现了 {token}"


def test_silver_delete_covers_the_three_silver_tables():
    assert len(SILVER_DELETE_STATEMENTS) == 3
    for statement, table in zip(
        SILVER_DELETE_STATEMENTS, ("tmall_user_events", "tmall_repurchase_samples", "tmall_users")
    ):
        assert statement.strip().upper() == f"DELETE FROM {table}".upper()


def test_silver_delete_never_touches_the_run_ledger():
    """台账不能被导入流程清空——它就是「谁导过什么」的唯一记录。"""
    for statement in SILVER_DELETE_STATEMENTS:
        assert "tmall_ingestion_runs" not in statement


def test_gold_consistency_pairs_reference_real_tables():
    for name, gold_sql, silver_sql in GOLD_CONSISTENCY_SQL:
        for sql in (gold_sql, silver_sql):
            assert _referenced_tables(sql) <= TMALL_TABLE_NAMES, f"{name} 引用了非天猫表"


def test_gold_consistency_covers_every_gold_table():
    """每张 Gold 表都要有一条「汇总数 == 明细数」的对照。

    漏掉一张，那张表的刷新错误就永远不会被发现——它看起来只是
    「数字有点怪」，而没有任何断言会说它错。
    """
    covered = " ".join(name for name, _, _ in GOLD_CONSISTENCY_SQL)
    for name, _ in GOLD_TABLES:
        assert name.split("_")[1] in covered, f"缺少 {name} 的一致性对照"


def test_orphan_queries_only_touch_tmall_tables():
    for sql in (analytics.ORPHAN_EVENT_USERS_SQL, analytics.ORPHAN_REPURCHASE_USERS_SQL):
        assert _referenced_tables(sql) <= TMALL_TABLE_NAMES


# --------------------------------------------------------------------------
# 「复购」不能被算成「购买广度」
# --------------------------------------------------------------------------
#
# 下面这几条是**静态**断言：它们不连数据库，只看 SQL 的结构。
# 数字层面的验证在 tests/test_tmall_ingest_integration.py 里
# （那里有真数据库，能断言「同一商家买两次」确实判成复购）。
# 两层都要有：静态断言定位快，集成测试证明口径真的跑得对。


def test_user_repeat_buy_aggregates_buy_by_user_and_merchant():
    """复购必须先按「用户 × 商家」聚合，再判断有没有哪一家买过 ≥2 次。

    直接在明细上 `COUNT(DISTINCT merchant_id) >= 2` 得到的是**购买广度**：
    「买到 10 家店各买 1 次」会被算成复购用户，而那种人一次复购都没有。
    """
    sql = analytics.USER_METRICS_SQL

    # 存在按 (user_id, merchant_id) 聚合 buy 的 CTE
    assert "GROUP BY user_id, merchant_id" in sql
    assert "action_type = 'buy'" in sql
    # 判定的是「同一商家的购买次数」，不是「不同商家的个数」
    assert "merchant_buy_count >= 2" in sql
    assert "COUNT(DISTINCT merchant_id) FILTER (WHERE action_type = 'buy')) >= 2" not in sql


def test_user_metrics_has_both_flags_and_they_mean_different_things():
    """复购与购买广度必须是两个字段，不能共用一个名字。"""
    sql = analytics.USER_METRICS_SQL
    assert "multi_merchant_buy_flag" in sql
    assert "repeat_buy_flag" in sql
    # 广度来自「买过的商家数」，复购来自「同一商家的购买次数」
    assert "buy_merchant_count, 0) >= 2" in sql
    assert "repeat_buy_merchant_count, 0) >= 1" in sql


def test_merchant_metrics_exposes_repeat_buy_user_count_and_rate():
    sql = analytics._MERCHANT_DIMENSION_SQL
    assert "repeat_buy_user_count" in sql
    assert "repeat_buy_user_rate" in sql
    # 复购率的分母是「购买用户数」而不是「全部行为用户数」——
    # 把从没买过的人放进分母会得到一个偏低的比率
    assert (
        "r.repeat_buy_user_count::numeric\n"
        "           / NULLIF(COUNT(DISTINCT e.user_id) FILTER (WHERE e.action_type = 'buy'), 0)"
        in sql
    )


def test_category_metrics_has_no_repeat_buy_columns():
    """类目维度不重复购。

    「在同一家店买两次」是复购；同一个类目下的两次购买来自两家不同的店，
    那是品类偏好。给类目也算一个复购率会造出一个无法解释的数字。
    """
    sql = analytics.CATEGORY_METRICS_SQL
    assert "repeat_buy_user_count" not in sql
    assert "repeat_buy_user_rate" not in sql


def test_positive_rate_comes_from_split_totals_not_from_each_group():
    """正样本占比要从「整个 train 集」取，不能按 label 分组各算各的。

    按分组各算的话，negative 那一组算出来恒等于 0——
    于是 `WHERE dataset_split='train'` 会同时拿到 0.5 和 0，
    而 0 看起来完全像个合法答案。
    """
    sql = analytics.REPURCHASE_METRICS_SQL
    assert "split_positive_count" in sql
    assert "split_sample_count" in sql
    # 旧的错误写法：在分组内用 FILTER 数正样本，分母是窗口和
    assert "COUNT(*) FILTER (WHERE s.label = 1))::numeric" not in sql


def test_positive_rate_is_null_for_the_unlabeled_split():
    """test 集没有标签，占比必须是 NULL。

    `CASE WHEN s.dataset_split = 'train'` 这一支保证只有 train 会算出值，
    其余情况（test 的 unlabeled 行）为 NULL。
    """
    sql = analytics.REPURCHASE_METRICS_SQL
    assert "CASE WHEN s.dataset_split = 'train'" in sql
    assert "END AS positive_rate" in sql


# --------------------------------------------------------------------------
# 目录口径：正样本占比必须显式限定到 positive 行
# --------------------------------------------------------------------------


def test_catalog_positive_rate_formula_pins_both_split_and_label_group():
    """目录里的公式必须同时限定 dataset_split 和 label_group。

    只写 `dataset_split = 'train'` 会同时匹配 positive 与 negative 两行。
    虽然现在两行同值（所以怎么取都对），但把口径写完整能防住将来
    有人改回「按组各算」时又出现一个 0。
    """
    from app.agent.data_query.catalog import METRICS

    metric = next(m for m in METRICS if m["name"] == "tmall_repurchase_positive_rate")
    assert "dataset_split = 'train'" in metric["formula"]
    assert "label_group = 'positive'" in metric["formula"]


def test_catalog_separates_repeat_buy_from_purchase_breadth():
    """目录里复购与购买广度必须是两个指标，描述不能互相串。"""
    from app.agent.data_query.catalog import METRICS

    by_name = {m["name"]: m for m in METRICS}

    repeat = by_name["tmall_merchant_repeat_buy_user_count"]["definition"]
    assert "同一个商家" in repeat
    assert "2 条及以上" in repeat

    breadth = by_name["tmall_user_multi_merchant_buy_flag"]["definition"]
    assert "不同商家" in breadth
    assert "不是复购" in breadth


def test_catalog_buy_count_fields_say_they_are_not_orders():
    """buy 是行为记录不是订单——这一点必须在目录里写清楚。

    数据里没有订单号，多条 buy 无法归并成订单；任何叫「订单数」的天猫
    指标都是凭空捏造的。
    """
    from app.agent.data_query.catalog import DATASETS

    merchant = next(d for d in DATASETS if d["name"] == "tmall_merchant_metrics")
    assert "不是订单" in merchant["fields"]["buy_count"]
    assert "不是订单" in merchant["fields"]["repeat_buy_user_count"]
