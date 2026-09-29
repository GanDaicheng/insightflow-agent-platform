"""天猫 Silver / Gold 表的结构性测试。

只检查 SQLAlchemy metadata，不连接数据库。真实建表结果由
`alembic upgrade head` 与 scripts/verify_tmall_data.py 验证。
"""

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models import Base
from app.models.tmall import (
    ACTION_TYPES,
    DATASET_SPLITS,
    GOLD_TABLES,
    INGESTION_STATUSES,
    LABEL_GROUPS,
    SILVER_TABLES,
    TmallCategoryMetric,
    TmallDailyMetric,
    TmallFunnelMetric,
    TmallIngestionRun,
    TmallMerchantMetric,
    TmallRepurchaseSample,
    TmallUserEvent,
    TmallUserMetric,
)

EXPECTED_SILVER = {"tmall_ingestion_runs", "tmall_users", "tmall_user_events",
                   "tmall_repurchase_samples"}
EXPECTED_GOLD = {
    "tmall_daily_metrics",
    "tmall_merchant_metrics",
    "tmall_category_metrics",
    "tmall_user_metrics",
    "tmall_funnel_metrics",
    "tmall_repurchase_metrics",
}


def _check_names(table) -> set[str]:
    return {c.name for c in table.constraints if isinstance(c, CheckConstraint)}


def _check_sql(table, name: str) -> str:
    constraint = next(c for c in table.constraints if c.name == name)
    return str(constraint.sqltext)


def _unique_names(table) -> set[str]:
    return {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}


def _index_names(table) -> set[str]:
    return {i.name for i in table.indexes}


def test_all_eleven_tmall_tables_are_registered():
    assert EXPECTED_SILVER | EXPECTED_GOLD <= set(Base.metadata.tables)
    assert len(EXPECTED_SILVER | EXPECTED_GOLD) == 10


def test_registry_constants_match_the_expected_sets():
    """SILVER_TABLES / GOLD_TABLES 是导入与校验脚本的唯一来源，不能和实际表脱节。"""
    assert {name for name, _ in SILVER_TABLES} == {"tmall_users", "tmall_user_events",
                                                   "tmall_repurchase_samples"}
    assert {name for name, _ in GOLD_TABLES} == EXPECTED_GOLD
    for name, model in SILVER_TABLES + GOLD_TABLES:
        assert model.__tablename__ == name
        assert Base.metadata.tables[name] is model.__table__


def test_ingestion_run_has_no_unique_constraint():
    """台账表要允许同一份文件留下多条记录（失败、重试各一条）。

    加了唯一约束就会把「上次为什么失败」覆盖掉，而那正是这张表存在的理由。
    幂等判定靠查询 status='succeeded'，不靠约束。
    """
    assert _unique_names(TmallIngestionRun.__table__) == set()


def test_ingestion_run_indexes_the_idempotency_key():
    """幂等判定按 (sha256, modulus, residue, status) 查，必须有索引。"""
    assert "ix_tmall_ingestion_runs_identity" in _index_names(TmallIngestionRun.__table__)


def test_ingestion_run_records_every_required_field():
    columns = set(TmallIngestionRun.__table__.c.keys())
    assert {
        "run_id",
        "file_name",
        "sha256",
        "sample_modulus",
        "sample_residue",
        "status",
        "raw_row_count",
        "imported_row_count",
        "started_at",
        "finished_at",
        "error_category",
        "error_message",
    } <= columns


def test_ingestion_run_status_and_sampling_are_constrained():
    assert "ck_tmall_ingestion_runs_status" in _check_names(TmallIngestionRun.__table__)
    assert "ck_tmall_ingestion_runs_sample_residue_range" in _check_names(
        TmallIngestionRun.__table__
    )
    status_sql = _check_sql(TmallIngestionRun.__table__, "ck_tmall_ingestion_runs_status")
    for status in INGESTION_STATUSES:
        assert status in status_sql


def test_run_and_silver_counts_are_bigint_or_larger():
    """行数最多到千万级，Integer 放不下。"""
    for table, column in (
        (TmallIngestionRun, "raw_row_count"),
        (TmallIngestionRun, "imported_row_count"),
        (TmallUserEvent, "source_row_number"),
    ):
        assert table.__table__.c[column].type.__class__.__name__ == "BigInteger"


def test_users_table_has_only_three_columns():
    """用户表刻意只有画像两列，没有任何直接标识。"""
    assert set(Base.metadata.tables["tmall_users"].c.keys()) == {
        "user_id",
        "age_range",
        "gender",
    }


def test_user_event_renames_seller_and_category():
    """seller_id → merchant_id、cat_id → category_id 是全局约定。

    原始列名如果残留在模型里，下游按 merchant_id 写的 SQL 会直接找不到列；
    更糟的是有人可能同时用两个名字，写出一个查不到数据的「对的」查询。
    """
    columns = set(TmallUserEvent.__table__.c.keys())
    assert {"merchant_id", "category_id"} <= columns
    assert "seller_id" not in columns
    assert "cat_id" not in columns


def test_user_event_columns_match_the_silver_contract():
    assert set(TmallUserEvent.__table__.c.keys()) == {
        "event_id",
        "source_row_number",
        "user_id",
        "item_id",
        "category_id",
        "merchant_id",
        "brand_id",
        "event_date",
        "action_type",
    }


def test_brand_id_is_nullable_but_the_rest_of_the_grain_is_not():
    table = TmallUserEvent.__table__
    assert table.c.brand_id.nullable is True
    for column in ("user_id", "item_id", "category_id", "merchant_id", "event_date",
                   "action_type", "source_row_number"):
        assert table.c[column].nullable is False, f"{column} 不应可空"


def test_source_row_number_is_unique():
    assert TmallUserEvent.__table__.c.source_row_number.unique is True


def test_action_type_is_constrained_to_the_four_actions():
    sql = _check_sql(TmallUserEvent.__table__, "ck_tmall_user_events_action_type")
    for action in ACTION_TYPES:
        assert action in sql
    assert _check_names(TmallUserEvent.__table__) >= {"ck_tmall_user_events_action_type"}


def test_event_indexes_support_the_gold_aggregations():
    """Gold 层全部按这几列分组，缺一个索引就是一次全表扫。"""
    indexes = _index_names(TmallUserEvent.__table__)
    for column in ("event_date", "action_type", "merchant_id", "category_id", "user_id",
                   "item_id", "source_row_number"):
        assert f"ix_tmall_user_events_{column}" in indexes, f"缺少 {column} 的索引"


def test_repurchase_uniqueness_is_user_merchant_split():
    assert "uq_tmall_repurchase_samples_user_id_merchant_id_dataset_split" in _unique_names(
        TmallRepurchaseSample.__table__
    )


def test_repurchase_label_and_probability_follow_the_split():
    """train 行有 label 没有 prob；test 行没有 label，prob 可空。

    test 的 prob 可空不是宽松，是**必须**：官方发布的 test_format1.csv
    里 prob 列整列为空。写成「test 必须有 prob」会让整份数据导不进来，
    而且报错位置在 COPY，离病根很远。

    这条约束真正防的是：把 prob 错写进 label。那样按 label 统计的
    正样本占比会是一个看起来合理但完全错误的数字。
    """
    names = _check_names(TmallRepurchaseSample.__table__)
    assert "ck_tmall_repurchase_samples_label_split_exclusive" in names
    sql = _check_sql(
        TmallRepurchaseSample.__table__,
        "ck_tmall_repurchase_samples_label_split_exclusive",
    )
    assert "train" in sql and "test" in sql
    # test 那一支不能要求 probability IS NOT NULL
    test_branch = sql.split("test")[1]
    assert "probability IS NOT NULL" not in test_branch
    assert TmallRepurchaseSample.__table__.c.label.nullable is True
    assert TmallRepurchaseSample.__table__.c.probability.nullable is True


def test_repurchase_split_and_label_are_constrained():
    names = _check_names(TmallRepurchaseSample.__table__)
    assert "ck_tmall_repurchase_samples_dataset_split" in names
    assert "ck_tmall_repurchase_samples_label_binary" in names
    assert "ck_tmall_repurchase_samples_probability_unit_interval" in names
    split_sql = _check_sql(
        TmallRepurchaseSample.__table__, "ck_tmall_repurchase_samples_dataset_split"
    )
    for split in DATASET_SPLITS:
        assert split in split_sql


def test_funnel_has_exactly_one_row_per_action_via_primary_key():
    assert list(TmallFunnelMetric.__table__.primary_key.columns.keys()) == ["action_type"]
    assert "ck_tmall_funnel_metrics_step_order_range" in _check_names(
        TmallFunnelMetric.__table__
    )


def test_daily_metrics_grain_is_date_and_action():
    assert list(TmallDailyMetric.__table__.primary_key.columns.keys()) == [
        "metric_date",
        "action_type",
    ]


def test_user_metrics_grain_is_user():
    assert list(TmallUserMetric.__table__.primary_key.columns.keys()) == ["user_id"]
    columns = set(TmallUserMetric.__table__.c.keys())
    assert {"multi_merchant_buy_flag", "repeat_buy_flag",
            "buy_merchant_count", "active_days",
            "first_event_date", "last_event_date"} <= columns


def test_repeat_buy_and_purchase_breadth_are_separate_columns():
    """复购与购买广度必须是两个字段。

    它们曾经共用一个 `repeat_buy_flag`，而那个字段算的是**广度**——
    买到 10 家店各买 1 次的人会被标成复购用户，尽管他一次复购都没有。
    现在 `repeat_buy_flag` 只表示「在同一个商家买过 ≥2 次」，
    广度由名字里写明 multi_merchant 的那个字段承担。
    """
    columns = set(TmallUserMetric.__table__.c.keys())
    assert "repeat_buy_flag" in columns
    assert "multi_merchant_buy_flag" in columns

    # 文档里也要把两者的区别说清楚，不能只靠字段名
    doc = TmallUserMetric.__doc__ or ""
    assert "不同的商家" in doc or "不同商家" in doc
    assert "复购" in doc


def test_merchant_metrics_has_repeat_buy_user_count_and_rate():
    columns = set(TmallMerchantMetric.__table__.c.keys())
    assert {"repeat_buy_user_count", "repeat_buy_user_rate"} <= columns
    for column in ("repeat_buy_user_count", "repeat_buy_user_rate"):
        assert TmallMerchantMetric.__table__.c[column].nullable is False


def test_merchant_repeat_buy_user_count_cannot_exceed_buy_users():
    """「买过 ≥2 次」是「买过 ≥1 次」的子集，约束恒成立。

    它的价值是在刷新 SQL 写错聚合口径时当场拒绝——
    比如误用 COUNT(DISTINCT user_id) 就会写出一个大于分母的比率，
    而这种错误在报表上看起来只是个「有点高的复购率」。
    """
    constraint = "ck_tmall_merchant_metrics_repeat_buy_user_count_within_buy_users"
    assert constraint in _check_names(TmallMerchantMetric.__table__)
    sql = _check_sql(TmallMerchantMetric.__table__, constraint)
    assert "repeat_buy_user_count <= buy_user_count" in sql


def test_category_metrics_has_no_repeat_buy_columns():
    """类目维度不重复购：同一类目下的两次购买来自两家不同的店。"""
    columns = set(TmallCategoryMetric.__table__.c.keys())
    assert "repeat_buy_user_count" not in columns
    assert "repeat_buy_user_rate" not in columns
    assert "repeat_buy_flag" not in columns


def test_label_groups_cover_the_unlabeled_test_split():
    """test 集没有标签，用 unlabeled 显式表示，不用 label = -1 的哨兵值。"""
    assert LABEL_GROUPS == ("positive", "negative", "unlabeled")
    assert "unlabeled" in LABEL_GROUPS


def test_no_gold_table_contains_a_user_level_event_column():
    """Gold 层不能出现商品/品牌级的明细列。

    一旦 Gold 里多了 item_id 这种列，粒度就变成「用户 × 商品」，
    行数会逼近 Silver，访问控制的意义也就没了。
    """
    forbidden = {"item_id", "brand_id", "event_id", "source_row_number"}
    for name, model in GOLD_TABLES:
        columns = set(model.__table__.c.keys())
        assert not (columns & forbidden), f"{name} 出现了明细级列：{columns & forbidden}"


def test_no_monetary_columns_exist_anywhere():
    """这份数据集没有价格信息，任何金额列都是凭空捏造的。"""
    forbidden_keywords = ("amount", "price", "revenue", "gmv", "profit", "cost", "quantity")
    for table in Base.metadata.tables.values():
        if not table.name.startswith("tmall"):
            continue
        for column in table.c.keys():
            lowered = column.lower()
            for keyword in forbidden_keywords:
                assert keyword not in lowered, f"{table.name}.{column} 疑似金额/数量列"
