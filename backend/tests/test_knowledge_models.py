"""知识库模型与迁移的静态测试。

**本文件不连接数据库。** 断言的都是「模型定义」和「迁移文件内容」——
这两样在 Python 里就能检查干净，不需要一个真库在旁边。
需要真实数据库才能验证的部分（列类型真的是 vector(1024)、外键真的级联删除）
由 alembic 迁移时的 DDL 渲染和一次性实测覆盖，不放进常规测试套件——
保持「pytest 不需要 PostgreSQL」这条项目约定。

这里守三类风险：
1. 模型定义漂移：列类型、可空性、约束名被无意改掉；
2. 迁移文件写坏：忘了幂等的 CREATE EXTENSION、回滚时误删扩展、加了不该加的向量索引；
3. 维度三处不同步：模型 / 迁移 / .env。
"""

import ast
import pathlib

import pytest
from sqlalchemy import BigInteger, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.core.config import get_settings
from app.models import Base
from app.models.knowledge import (
    CONTENT_HASH_LENGTH,
    KNOWLEDGE_EMBEDDING_DIMENSIONS,
    SEARCH_TEXT_TRGM_INDEX,
    KnowledgeChunk,
    KnowledgeDocument,
    Vector,
)

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"

# 建知识库两张表的迁移（本文件大部分断言针对它）
KNOWLEDGE_SCHEMA_REVISION = "69e6c2579c1b"
KNOWLEDGE_SCHEMA_MIGRATION_FILE = (
    VERSIONS_DIR / f"{KNOWLEDGE_SCHEMA_REVISION}_create_knowledge_base_schema.py"
)
# 它前面那个迁移：建 5 张零售表
RETAIL_SCHEMA_REVISION = "e205344666e8"

# 检索元数据列。本阶段只建字段，没有代码往里写。
RETRIEVAL_METADATA_COLUMNS = {"keywords", "aliases", "search_text"}

# 迁移之前的 5 张零售表 + alembic 自己的版本表，字段一个都不该变
RETAIL_TABLE_COLUMNS = {
    "customers": {"customer_id", "customer_name", "member_level", "registered_at", "created_at"},
    "products": {"product_id", "product_name", "category_name", "unit_price", "cost_price", "created_at"},
    "regions": {"region_id", "region_name", "region_level", "created_at"},
    "date_dim": {
        "date_id", "full_date", "year", "quarter", "month",
        "month_name", "day_of_month", "week_of_year", "is_weekend",
    },
    "orders": {
        "order_id", "order_no", "customer_id", "product_id", "region_id", "date_id",
        "quantity", "unit_price", "gross_amount", "discount_amount", "net_amount",
    },
}


def migration_source(path: pathlib.Path) -> str:
    """迁移文件原文。只用于「文件存在/能读」这类不敏感的断言。"""
    return path.read_text(encoding="utf-8")


def migration_module_assignments(path: pathlib.Path) -> dict[str, str]:
    """迁移文件顶层的赋值语句，例如 revision / down_revision / 维度常量。

    为什么不直接对原文做正则？因为注释会一起被命中。
    这个坑当场踩到了：迁移注释里写了「本迁移不创建 HNSW / IVFFlat 索引」，
    于是「不该出现 hnsw」那条测试反而因为注释写得清楚而失败。
    要看代码就看 AST，别拿字符串猜。
    """
    tree = ast.parse(migration_source(path))
    assignments: dict[str, str] = {}
    for node in tree.body:
        # revision / down_revision 是带类型注解的赋值（AnnAssign），
        # 而 EMBEDDING_DIMENSIONS 是普通赋值（Assign）——两种都要收。
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments[node.target.id] = ast.unparse(node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = ast.unparse(node.value)
    return assignments


def function_calls(path: pathlib.Path, function_name: str) -> list[str]:
    """某个函数体内所有调用的源码文本（注释天然不会出现在这里）。"""
    tree = ast.parse(migration_source(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return [
                ast.unparse(child) for child in ast.walk(node) if isinstance(child, ast.Call)
            ]
    raise AssertionError(f"迁移里没有找到 {function_name}()")


def upgrade_calls() -> list[str]:
    return function_calls(KNOWLEDGE_SCHEMA_MIGRATION_FILE, "upgrade")


def downgrade_calls() -> list[str]:
    return function_calls(KNOWLEDGE_SCHEMA_MIGRATION_FILE, "downgrade")


# --------------------------------------------------------------------------
# 模型已登记进 metadata（env.py 只导入 app.models，漏登记 autogenerate 就看不见）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["knowledge_documents", "knowledge_chunks"])
def test_new_tables_are_registered_in_metadata(table_name):
    assert table_name in Base.metadata.tables


def test_models_are_exported_from_the_models_package():
    import app.models as models

    assert models.KnowledgeDocument is KnowledgeDocument
    assert models.KnowledgeChunk is KnowledgeChunk
    assert models.KNOWLEDGE_EMBEDDING_DIMENSIONS == KNOWLEDGE_EMBEDDING_DIMENSIONS


# --------------------------------------------------------------------------
# knowledge_documents
# --------------------------------------------------------------------------


def test_documents_table_columns():
    table = Base.metadata.tables["knowledge_documents"]

    assert set(table.c.keys()) == {
        "id", "source_file", "document_title", "content_hash",
        "chunk_count", "created_at", "updated_at",
    }


def test_documents_primary_key_is_bigint_autoincrement():
    column = Base.metadata.tables["knowledge_documents"].c.id

    assert isinstance(column.type, BigInteger)
    assert column.primary_key
    assert column.autoincrement is True


def test_source_file_is_unique_and_not_nullable():
    column = Base.metadata.tables["knowledge_documents"].c.source_file

    assert column.nullable is False
    assert column.unique is True


def test_content_hash_columns_are_long_enough_for_sha256():
    """sha256 十六进制是 64 字符。列写短了会在入库时被静默截断或直接报错。"""
    assert CONTENT_HASH_LENGTH == 64

    documents = Base.metadata.tables["knowledge_documents"].c.content_hash
    chunks = Base.metadata.tables["knowledge_chunks"].c.content_hash

    assert documents.type.length == CONTENT_HASH_LENGTH
    assert chunks.type.length == CONTENT_HASH_LENGTH


@pytest.mark.parametrize("column_name", ["created_at", "updated_at"])
def test_timestamps_have_server_defaults(column_name):
    column = Base.metadata.tables["knowledge_documents"].c[column_name]

    assert column.nullable is False
    assert column.server_default is not None


# --------------------------------------------------------------------------
# knowledge_chunks
# --------------------------------------------------------------------------


def test_chunks_table_columns():
    table = Base.metadata.tables["knowledge_chunks"]

    assert set(table.c.keys()) == {
        "id", "document_id", "section_title", "chunk_index",
        "content", "content_for_embedding", "content_hash",
        "estimated_token_count", "char_count",
        "embedding", "embedding_model",
        "created_at", "updated_at",
    } | RETRIEVAL_METADATA_COLUMNS


def test_embedding_column_renders_as_vector_with_the_configured_dimension():
    """列类型必须渲染成 vector(1024)，而不是 TEXT 或没有长度的 vector。"""
    column = Base.metadata.tables["knowledge_chunks"].c.embedding

    assert isinstance(column.type, Vector)
    assert column.type.dimensions == KNOWLEDGE_EMBEDDING_DIMENSIONS
    assert str(column.type) == f"vector({KNOWLEDGE_EMBEDDING_DIMENSIONS})"


def test_embedding_column_is_nullable():
    """可空是有意的：支持「先落文本、后补向量」，接口超时不至于丢整批数据。"""
    assert Base.metadata.tables["knowledge_chunks"].c.embedding.nullable is True


def test_embedding_model_column_is_nullable():
    column = Base.metadata.tables["knowledge_chunks"].c.embedding_model

    assert column.nullable is True


def test_document_id_foreign_key_cascades_on_delete():
    table = Base.metadata.tables["knowledge_chunks"]
    foreign_keys = list(table.c.document_id.foreign_keys)

    assert len(foreign_keys) == 1
    target = foreign_keys[0]
    assert target.target_fullname == "knowledge_documents.id"
    assert target.ondelete == "CASCADE"


def test_chunk_unique_constraint_is_the_document_scoped_pair():
    """唯一性必须是 (document_id, chunk_index) 组合。

    如果误写成全局 content_hash 唯一，不同文档里一模一样的说明片段会让后者入库失败。
    """
    table = Base.metadata.tables["knowledge_chunks"]
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]

    assert len(uniques) == 1
    assert {column.name for column in uniques[0].columns} == {"document_id", "chunk_index"}
    assert uniques[0].name == "uq_knowledge_chunks_document_id_chunk_index"


def test_content_hash_is_indexed_but_not_unique():
    """content_hash 只建普通索引，不做全局唯一（去重策略留给后续）。"""
    table = Base.metadata.tables["knowledge_chunks"]
    indexes = {index.name: index for index in table.indexes}

    assert "ix_knowledge_chunks_content_hash" in indexes
    assert indexes["ix_knowledge_chunks_content_hash"].unique is False


def test_no_vector_index_is_created():
    """当前只有 44 个切片，精确扫描更合适；建 HNSW/IVFFlat 需要单独一个迁移。"""
    table = Base.metadata.tables["knowledge_chunks"]
    index_names = " ".join(index.name for index in table.indexes).lower()

    assert "hnsw" not in index_names
    assert "ivfflat" not in index_names


# --------------------------------------------------------------------------
# 检索元数据列：keywords / aliases / search_text
#
# 这三列本阶段只建结构，没有代码写入，所以测试全部是「定义是否正确」，
# 不涉及任何行为。真正往它们写数据是下一阶段的事。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("column_name", sorted({"keywords", "aliases"}))
def test_jsonb_metadata_columns_are_jsonb(column_name):
    """用 JSONB 而不是 JSON：JSONB 支持 GIN 索引和包含查询，JSON 两样都不行。"""
    column = Base.metadata.tables["knowledge_chunks"].c[column_name]

    assert isinstance(column.type, JSONB)


@pytest.mark.parametrize("column_name", sorted({"keywords", "aliases"}))
def test_jsonb_metadata_columns_are_not_nullable(column_name):
    """非空：后续召回会直接对这些列做包含查询，NULL 会让这些行静默漏掉。"""
    assert Base.metadata.tables["knowledge_chunks"].c[column_name].nullable is False


@pytest.mark.parametrize("column_name", sorted({"keywords", "aliases"}))
def test_jsonb_metadata_python_default_is_a_factory_not_a_shared_list(column_name):
    """必须是 default=list，不能是 default=[]。

    后者是**一个**列表对象，被所有实例共享：往一条切片上 append，
    另一条也跟着变。这种串味只在多实例共存时才暴露，最难排查。

    所以这里不只断言「是个可调用对象」，还真的调两次看返回的是不是同一个列表
    （SQLAlchemy 会把工厂包一层、多接一个上下文参数，所以用 None 当上下文）。
    """
    default = Base.metadata.tables["knowledge_chunks"].c[column_name].default

    assert default is not None
    assert default.is_callable is True
    assert default.is_scalar is False
    assert default.arg(None) == []
    assert default.arg(None) is not default.arg(None)


@pytest.mark.parametrize("column_name", sorted({"keywords", "aliases"}))
def test_jsonb_metadata_server_default_is_an_array_not_a_string(column_name):
    """数据库默认值要解析成 JSON 数组，不是字符串 "[]"。

    写漏 ::jsonb 转换、或者手滑写成 '"[]"'，库里存的就是 JSON 字符串；
    后续 jsonb_typeof(...) = 'array' 的检查会静默漏掉这些行。
    """
    arg = str(Base.metadata.tables["knowledge_chunks"].c[column_name].server_default.arg)

    assert arg == "'[]'::jsonb"


def test_search_text_is_text_and_not_nullable():
    column = Base.metadata.tables["knowledge_chunks"].c.search_text

    assert isinstance(column.type, Text)
    assert column.nullable is False


def test_search_text_has_an_empty_string_default():
    """Python 与数据库两侧的默认值都是空串，不是 NULL。

    在元数据提取接入之前，新切片只能靠这个默认值落库；
    给 NULL 的话要么插不进去，要么让后续「search_text = ''」的判断失灵。
    """
    column = Base.metadata.tables["knowledge_chunks"].c.search_text

    assert column.default.arg == ""
    assert str(column.server_default.arg) == "''"


def test_search_text_trigram_index_is_declared_on_the_model():
    """索引必须同时写在模型里，不能只写在迁移里。

    只写在迁移里的话，Base.metadata 与真实库不一致——以后 autogenerate
    会发现「库里有这个索引、模型里没有」，再生成一条删它的迁移。
    """
    table = Base.metadata.tables["knowledge_chunks"]
    indexes = {index.name: index for index in table.indexes}

    assert SEARCH_TEXT_TRGM_INDEX in indexes
    index = indexes[SEARCH_TEXT_TRGM_INDEX]
    assert [column.name for column in index.columns] == ["search_text"]

    options = index.dialect_options["postgresql"]
    assert options["using"] == "gin"
    assert options["ops"] == {"search_text": "gin_trgm_ops"}


@pytest.mark.parametrize(
    "constraint_name",
    [
        "ck_knowledge_chunks_char_count_not_negative",
        "ck_knowledge_chunks_chunk_index_not_negative",
        "ck_knowledge_chunks_estimated_token_count_not_negative",
        "ck_knowledge_documents_chunk_count_not_negative",
    ],
)
def test_non_negative_check_constraints_exist(constraint_name):
    all_names = {
        constraint.name
        for table in ("knowledge_documents", "knowledge_chunks")
        for constraint in Base.metadata.tables[table].constraints
    }
    assert constraint_name in all_names


# --------------------------------------------------------------------------
# Vector 类型的序列化处理器（纯函数，不连库）
# --------------------------------------------------------------------------


def test_vector_get_col_spec_reports_the_dimension():
    assert Vector(1024).get_col_spec() == "vector(1024)"
    assert Vector(768).get_col_spec() == "vector(768)"


def test_vector_bind_processor_serializes_python_floats():
    process = Vector(3).bind_processor(None)

    assert process([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"
    assert process([1, 2, 3]) == "[1.0,2.0,3.0]"


def test_vector_bind_processor_passes_through_none_and_strings():
    process = Vector(3).bind_processor(None)

    assert process(None) is None
    # 已经是 pgvector 文本格式时原样放行，便于直接执行 SQL 的场景
    assert process("[0.1,0.2,0.3]") == "[0.1,0.2,0.3]"


def test_vector_result_processor_parses_back_to_floats():
    process = Vector(3).result_processor(None, None)

    assert process("[0.1,0.2,0.3]") == [0.1, 0.2, 0.3]
    assert process(None) is None
    assert process("[]") == []


def test_vector_round_trip_preserves_values():
    """写进去再读出来必须完全一致——这是入库和检索都依赖的性质。"""
    bind = Vector(4).bind_processor(None)
    result = Vector(4).result_processor(None, None)

    original = [0.125, -1.5, 3.0, 0.0]

    assert result(bind(original)) == original


def test_vector_type_is_cacheable():
    """cache_ok=False 会让 SQLAlchemy 每次编译语句都重新生成 SQL，白费性能。"""
    assert Vector(1024).cache_ok is True


# --------------------------------------------------------------------------
# 维度三处必须同步：模型 / 迁移 / .env
# --------------------------------------------------------------------------


def test_model_dimension_matches_configured_embedding_dimension():
    """模型里的 vector(N) 必须与 .env 的 EMBEDDING_DIMENSION 一致。

    这两处一旦不一致，插入时会报「expected N dimensions」，
    而报错发生在入库那一刻、离改配置的那一刻很远。在这里提前拦住。
    改维度需要三步同时做：改 .env、改模型常量、新写一个迁移改列类型
    （PG 不允许直接改 vector 的长度）。"""
    configured = get_settings().embedding_dimension

    assert KNOWLEDGE_EMBEDDING_DIMENSIONS == configured, (
        f"模型是 vector({KNOWLEDGE_EMBEDDING_DIMENSIONS})，"
        f"但 .env 的 EMBEDDING_DIMENSION 是 {configured}。"
        "改维度要同步：.env、app/models/knowledge.py、以及一个改列类型的新迁移。"
    )


# --------------------------------------------------------------------------
# 迁移文件
# --------------------------------------------------------------------------


def test_knowledge_schema_migration_chains_onto_the_retail_schema():
    assignments = migration_module_assignments(KNOWLEDGE_SCHEMA_MIGRATION_FILE)

    assert assignments["revision"] == f"'{KNOWLEDGE_SCHEMA_REVISION}'"
    assert assignments["down_revision"] == f"'{RETAIL_SCHEMA_REVISION}'"


def test_existing_migration_is_untouched():
    """已有迁移必须保持冻结——它记录的是当时发生了什么，改了就无法重放。"""
    path = VERSIONS_DIR / f"{RETAIL_SCHEMA_REVISION}_create_retail_analytics_schema.py"
    assignments = migration_module_assignments(path)

    assert assignments["revision"] == f"'{RETAIL_SCHEMA_REVISION}'"
    assert assignments["down_revision"] == "None"


def test_migration_chain_has_exactly_one_head():
    """迁移链只能有一个头；出现分叉时 upgrade head 会报错，谁也不知道该走哪条。

    这里断言的是「只有一个」，不是「一定是哪一条」——具体是哪条
    由每条迁移自己的测试负责（见 test_rag_search_metadata_migration.py）。
    每加一条迁移，head 就会前移一次，把具体 revision 写死在这里会变成无谓的维护。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ALEMBIC_INI))
    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1


def test_migration_creates_the_vector_extension_idempotently():
    """迁移自己保证扩展存在，否则干净机器上 upgrade head 会失败。"""
    assert any(
        "CREATE EXTENSION IF NOT EXISTS vector" in call for call in upgrade_calls()
    )


def test_migration_downgrade_does_not_drop_the_extension():
    """回滚知识库不该顺带废掉别人的向量列——扩展是库级别共享对象。"""
    assert not any("DROP EXTENSION" in call.upper() for call in downgrade_calls())


def test_migration_creates_both_tables_and_an_embedding_column():
    calls = upgrade_calls()

    assert any("'knowledge_documents'" in call for call in calls)
    assert any("'knowledge_chunks'" in call for call in calls)
    # 列类型由 VectorType 渲染，维度取自模块常量
    assert any("VectorType(EMBEDDING_DIMENSIONS)" in call for call in calls)
    assert (
        migration_module_assignments(KNOWLEDGE_SCHEMA_MIGRATION_FILE)["EMBEDDING_DIMENSIONS"]
        == "1024"
    )


def test_migration_does_not_create_a_vector_index():
    """建表语句里不该出现向量索引；要加索引得单独写一个迁移。"""
    joined = " ".join(upgrade_calls()).lower()

    assert "hnsw" not in joined
    assert "ivfflat" not in joined


def test_migration_does_not_touch_the_retail_tables():
    """本迁移只新增两张知识库表，不得出现删除或修改零售表的语句。

    两种查法都保留：AST 调用列表查的是「执行的语句」，
    源码文本查的是「整段 upgrade 提到过什么」——后者更严，
    连注释和字符串里的表名都拦得住。
    """
    calls = " ".join(upgrade_calls())
    source = migration_source(KNOWLEDGE_SCHEMA_MIGRATION_FILE)
    upgrade_body = source.split("def downgrade()")[0]

    for table in RETAIL_TABLE_COLUMNS:
        assert f"drop_table('{table}')" not in calls
        assert f"'{table}'" not in calls, f"upgrade 里提到了零售表 {table}"
        assert f"drop_table('{table}')" not in upgrade_body
        assert f'"{table}"' not in upgrade_body, f"upgrade 里提到了零售表 {table}"


# --------------------------------------------------------------------------
# 零售模型未被改动
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("table_name", "expected"), RETAIL_TABLE_COLUMNS.items())
def test_retail_table_columns_are_unchanged(table_name, expected):
    """这 5 张表的字段一个都不该动。加了或删了列都必须是有意识的行为，
    这个测试就是那道「有意识」的关卡。"""
    assert set(Base.metadata.tables[table_name].c.keys()) == expected


def test_retail_member_levels_are_still_the_four_known_values():
    from app.models import MEMBER_LEVELS

    assert MEMBER_LEVELS == ("普通会员", "银卡会员", "金卡会员", "黑金会员")
