"""ORM 模型定义的结构性测试。

只检查 SQLAlchemy metadata，不连接数据库：表、主键、外键、唯一约束、
检查约束、索引是否都按设计声明齐全。真实建表结果由 alembic upgrade head 验证。
"""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

from app.models import Base
from app.models.retail import MEMBER_LEVELS, Customer, DateDim, Order, Product, Region
from app.models.tmall import GOLD_TABLES as TMALL_GOLD_TABLES
from app.models.tmall import SILVER_TABLES as TMALL_SILVER_TABLES

EXPECTED_TABLES = {
    "customers", "products", "regions", "date_dim", "orders", "channels", "promotions",
    "order_operations", "inventory_snapshots", "ad_campaigns", "ad_daily_metrics", "after_sales",
}

# RAG 阶段新增的两张知识库表。它们由 tests/test_knowledge_models.py 专门覆盖，
# 这里登记一份是为了让「表集合」这条断言保持完整——
# 任何新表都必须在这里显式登记一次，加表就成了有意识的行为。
KNOWLEDGE_TABLES = {"knowledge_documents", "knowledge_chunks"}

# 天猫数据集接入阶段新增的表。它由 tests/test_tmall_models.py 专门覆盖，
# 这里同样只登记一份，理由与上面相同。
TMALL_TABLES = (
    {"tmall_ingestion_runs"} | {name for name, _ in TMALL_SILVER_TABLES}
    | {name for name, _ in TMALL_GOLD_TABLES}
)

# 任务书要求显式定义的索引，缺一不可。
# date_dim.full_date 不在这个集合里：它以 UNIQUE 约束的形式声明，
# 而 PostgreSQL 的 UNIQUE 约束本身就是靠唯一索引实现的（见文件末尾的专项测试）。
EXPECTED_INDEXES = {
    "ix_orders_date_id",
    "ix_orders_customer_id",
    "ix_orders_product_id",
    "ix_orders_region_id",
    "ix_orders_order_no",
    "ix_products_category_name",
    "ix_customers_member_level",
}

# 不应出现的个人信息字段名，命中任意一个都说明样例数据设计越界了
PERSONAL_INFO_COLUMN_KEYWORDS = (
    "phone",
    "mobile",
    "email",
    "address",
    "id_card",
    "idcard",
    "passport",
    "手机",
    "电话",
    "邮箱",
    "地址",
    "身份证",
)


def _check_constraint_names(table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _unique_constraint_names(table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _foreign_key_targets(table) -> set[str]:
    return {fk.target_fullname for fk in table.foreign_keys}


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def _all_columns():
    for table in Base.metadata.tables.values():
        for column in table.columns:
            yield table.name, column.name


def test_metadata_contains_exactly_the_expected_tables():
    """metadata 里只应有零售 5 张表 + 知识库 2 张表 + 天猫 10 张表。

    这条断言的价值是「新增表必须经过一次有意识的登记」：多出任何一张表都会失败，
    逼着加表的人回来想清楚它属于哪一类。

    原名叫 test_metadata_contains_exactly_the_five_target_tables——RAG 阶段加了
    知识库表之后，名字里的「五张」已经和断言对不上了，所以连同断言一起改掉。
    """
    assert set(Base.metadata.tables) == EXPECTED_TABLES | KNOWLEDGE_TABLES | TMALL_TABLES


def test_every_table_has_a_primary_key():
    for table in Base.metadata.tables.values():
        assert table.primary_key.columns, f"{table.name} 缺少主键"


def test_dimension_primary_keys_are_the_business_keys():
    assert list(Customer.__table__.primary_key.columns.keys()) == ["customer_id"]
    assert list(Product.__table__.primary_key.columns.keys()) == ["product_id"]
    assert list(Region.__table__.primary_key.columns.keys()) == ["region_id"]
    assert list(DateDim.__table__.primary_key.columns.keys()) == ["date_id"]
    assert list(Order.__table__.primary_key.columns.keys()) == ["order_id"]


def test_orders_foreign_keys_point_to_all_four_dimensions():
    assert _foreign_key_targets(Order.__table__) == {
        "customers.customer_id",
        "products.product_id",
        "regions.region_id",
        "date_dim.date_id",
    }


def test_orders_foreign_key_constraints_are_named():
    names = {
        constraint.name
        for constraint in Order.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert names == {
        "fk_orders_customer_id",
        "fk_orders_product_id",
        "fk_orders_region_id",
        "fk_orders_date_id",
    }


def test_unique_constraints_on_natural_keys():
    assert _unique_constraint_names(Region.__table__) == {"uq_regions_region_name"}
    assert _unique_constraint_names(DateDim.__table__) == {"uq_date_dim_full_date"}
    # orders.order_no 用唯一索引实现唯一性，见下面的索引测试
    assert Order.__table__.c.order_no.unique is True


def test_customers_member_level_check_constraint_lists_four_levels():
    names = _check_constraint_names(Customer.__table__)
    assert "ck_customers_member_level" in names

    constraint = next(
        c for c in Customer.__table__.constraints if c.name == "ck_customers_member_level"
    )
    sql = str(constraint.sqltext)
    for level in MEMBER_LEVELS:
        assert level in sql


def test_member_levels_are_the_four_required_levels():
    assert MEMBER_LEVELS == ("普通会员", "银卡会员", "金卡会员", "黑金会员")


def test_orders_check_constraints_cover_amount_rules():
    names = _check_constraint_names(Order.__table__)
    assert names == {
        "ck_orders_quantity_positive",
        "ck_orders_unit_price_not_negative",
        "ck_orders_gross_amount_not_negative",
        "ck_orders_discount_amount_not_negative",
        "ck_orders_discount_not_exceed_gross",
        "ck_orders_net_amount_consistent",
    }


def test_products_price_is_numeric_not_float():
    """金额必须用 Numeric（精确十进制），float 累加会产生分位误差。"""
    for table, column in ((Product, "unit_price"), (Order, "unit_price"),
                          (Order, "gross_amount"), (Order, "discount_amount"),
                          (Order, "net_amount")):
        column_type = table.__table__.c[column].type
        assert column_type.__class__.__name__ == "Numeric", f"{table.__name__}.{column} 不是 Numeric"
        assert column_type.scale == 2


def test_products_unit_price_must_be_positive():
    assert "ck_products_unit_price_positive" in _check_constraint_names(Product.__table__)


def test_date_dim_has_month_and_quarter_range_checks():
    assert _check_constraint_names(DateDim.__table__) == {
        "ck_date_dim_month_range",
        "ck_date_dim_quarter_range",
    }


def test_all_required_indexes_are_declared():
    declared: set[str] = set()
    for table in Base.metadata.tables.values():
        declared |= _index_names(table)

    missing = EXPECTED_INDEXES - declared
    assert not missing, f"以下索引未在模型中显式声明：{sorted(missing)}"


def test_orders_order_no_index_is_unique():
    index = next(i for i in Order.__table__.indexes if i.name == "ix_orders_order_no")
    assert isinstance(index, Index)
    assert index.unique is True


def test_date_dim_full_date_is_unique_and_therefore_indexed():
    """任务书要求 date_dim.full_date 有索引。

    这里用 UNIQUE 约束声明：PostgreSQL 会为它自动建立唯一索引
    （建表结果里就是 uq_date_dim_full_date），既保证了一一对应，
    也满足「按日期查维表要走索引」的要求。
    """
    assert "uq_date_dim_full_date" in _unique_constraint_names(DateDim.__table__)
    assert DateDim.__table__.c.full_date.unique is True


def test_no_personal_information_columns_exist():
    for table_name, column_name in _all_columns():
        lowered = column_name.lower()
        for keyword in PERSONAL_INFO_COLUMN_KEYWORDS:
            assert keyword not in lowered, f"{table_name}.{column_name} 疑似个人信息字段"
