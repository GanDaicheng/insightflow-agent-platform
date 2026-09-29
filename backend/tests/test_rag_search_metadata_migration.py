"""检索元数据迁移的静态测试。

**本文件不连接数据库。** 断言的对象是两样东西：
1. 迁移文件里的模块常量（直接 import 进来读真实值，不做字符串猜测）；
2. upgrade / downgrade 里**按源码顺序**排出的语句调用（走 AST，注释和文档字符串
   不会误伤——这个坑 test_knowledge_models.py 里踩过）。

需要真实数据库才能验证的部分（列真的是 JSONB、回填真的填上了标题、
索引真的建起来）由一次真实的 `alembic upgrade head` 加只读查询覆盖，
不放进每次都要跑的套件——保持「pytest 不需要 PostgreSQL」这条项目约定。

这里守三类风险：
1. 回填写坏：改了不该改的列（content / embedding / content_hash / updated_at）；
2. 顺序写错：downgrade 先删列再删索引（索引会被连带删掉，第二条语句直接报错）；
3. 共享对象被误删：downgrade 顺手把 pg_trgm 扩展干掉。
"""

import ast
import importlib.util
import pathlib
import re

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"

NEW_REVISION = "1e96e0de0042"
PREVIOUS_REVISION = "69e6c2579c1b"
MIGRATION_FILE = VERSIONS_DIR / f"{NEW_REVISION}_add_rag_search_metadata.py"

# 本迁移涉及的三个新列，升级时逐个新增、回滚时逐个删除
METADATA_COLUMNS = ("keywords", "aliases", "search_text")

# 5 张零售业务表。本迁移与它们无关，任何一条语句里都不该出现。
RETAIL_TABLES = ("customers", "products", "regions", "date_dim", "orders")

# 回填**不允许**写入的列。content 与 content_for_embedding 是回填的数据来源，
# 只能出现在等号右边，不能出现在左边。
COLUMNS_THAT_MUST_NOT_BE_WRITTEN = (
    "content",
    "content_for_embedding",
    "content_hash",
    "embedding",
    "embedding_model",
    "estimated_token_count",
    "char_count",
    "created_at",
    "updated_at",
)


def load_migration():
    """把迁移文件当普通模块导入，读它声明的常量。

    迁移模块只 import alembic.op 和 sqlalchemy，不连库，所以 import 是安全的；
    真实值比从源码里正则抠字符串可靠得多。
    """
    spec = importlib.util.spec_from_file_location("add_rag_search_metadata", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module_assignments() -> dict[str, str]:
    """迁移文件顶层的赋值语句（revision / down_revision / 各常量）。"""
    tree = ast.parse(MIGRATION_FILE.read_text(encoding="utf-8"))
    assignments: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments[node.target.id] = ast.unparse(node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = ast.unparse(node.value)
    return assignments


def ordered_calls(function_name: str) -> list[str]:
    """函数体内按**源码顺序**排出的调用表达式源码文本。

    不用 ast.walk 一把梭：它按广度优先遍历，语句一嵌套顺序就乱了，
    而本文件里有两处断言的就是顺序（回填要在建索引之前、删索引要在删列之前）。
    所以先按函数体的语句顺序走，再展开每条语句内部的调用。
    注释和文档字符串天然不会出现在结果里。
    """
    tree = ast.parse(MIGRATION_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            calls: list[str] = []
            for statement in node.body:
                calls.extend(
                    ast.unparse(child)
                    for child in ast.walk(statement)
                    if isinstance(child, ast.Call)
                )
            return calls
    raise AssertionError(f"迁移里没有找到 {function_name}()")


def upgrade_calls() -> list[str]:
    return ordered_calls("upgrade")


def downgrade_calls() -> list[str]:
    return ordered_calls("downgrade")


def upgrade_operations() -> list[str]:
    """upgrade 里真正下发给 Alembic 的操作，按顺序，不含 sa.Column 这类构造器。"""
    return [call for call in upgrade_calls() if call.startswith("op.")]


def downgrade_operations() -> list[str]:
    return [call for call in downgrade_calls() if call.startswith("op.")]


def add_column_call(column_name: str) -> str:
    """取某个新列的 add_column 调用文本。"""
    for call in upgrade_calls():
        if call.startswith("op.add_column(") and f"'{column_name}'" in call:
            return call
    raise AssertionError(f"upgrade 里没有新增 {column_name} 列")


# --------------------------------------------------------------------------
# 迁移链
# --------------------------------------------------------------------------


def test_migration_has_a_single_down_revision_pointing_at_the_knowledge_schema():
    """down_revision 必须来自实际 alembic head，而不是猜的。

    写成元组就说明有人以为存在分叉——那会让 upgrade head 变成多义操作。
    """
    assignments = module_assignments()

    assert assignments["revision"] == f"'{NEW_REVISION}'"
    assert assignments["down_revision"] == f"'{PREVIOUS_REVISION}'"


def test_migration_declares_no_branches_or_dependencies():
    assignments = module_assignments()

    assert assignments["branch_labels"] == "None"
    assert assignments["depends_on"] == "None"


def test_migration_is_the_single_alembic_head():
    """整个项目必须只有一个 head，否则 upgrade head 不知道该走哪条。

    这条断言每加一次迁移都要跟着改一次，而且**是故意这么设计的**：
    它逼着加迁移的人在合并前确认一次「链是直的，没有出现分叉」。
    head 变成新的那个，本身就说明新迁移挂对了位置。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ALEMBIC_INI))
    heads = ScriptDirectory.from_config(config).get_heads()

    # 迁移链：e205344666e8（零售）→ 69e6c2579c1b（知识库）→ 1e96e0de0042（RAG 元数据）
    #        → 20260924ba01（经营分析）→ 20260924ba02（运行标题）
    #        → bea2b5793f31（天猫 IJCAI 2015）→ 20260929ecom（电商运营扩展）
    assert list(heads) == ["20260929ecom"]


# --------------------------------------------------------------------------
# upgrade：扩展、列、回填、索引
# --------------------------------------------------------------------------


def test_upgrade_enables_pg_trgm_idempotently():
    """幂等写法：已装过 pg_trgm 的库上重复执行不能报错。"""
    assert any(
        "CREATE EXTENSION IF NOT EXISTS pg_trgm" in call for call in upgrade_calls()
    )


def test_upgrade_does_not_touch_the_vector_extension():
    """vector 是上一段功能的对象，本迁移不该碰它。"""
    joined = " ".join(upgrade_calls() + downgrade_calls())

    assert "vector" not in joined.lower()


@pytest.mark.parametrize("column_name", METADATA_COLUMNS)
def test_upgrade_adds_each_metadata_column(column_name):
    call = add_column_call(column_name)

    assert "op.add_column('knowledge_chunks'" in call


def test_upgrade_adds_exactly_three_columns():
    """只加这三个字段，不多不少。"""
    adds = [call for call in upgrade_operations() if call.startswith("op.add_column(")]

    assert len(adds) == len(METADATA_COLUMNS)


def test_upgrade_backfills_search_text():
    """回填的存在性：必须有一条独立的 UPDATE 语句，而不是只留下空列。"""
    executes = [call for call in upgrade_operations() if call.startswith("op.execute(")]

    assert any("SEARCH_TEXT_BACKFILL" in call for call in executes)


def test_backfill_is_a_single_update_statement():
    """回填只允许是一条 UPDATE。

    语句一多就很难一眼看出它到底改了什么，而「只改 search_text」
    是这个迁移最要紧的一条承诺。
    """
    sql = load_migration().SEARCH_TEXT_BACKFILL.strip().upper()

    assert sql.startswith("UPDATE KNOWLEDGE_CHUNKS")
    assert sql.count("UPDATE") == 1
    assert sql.count(";") <= 1
    for keyword in ("INSERT", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE"):
        assert keyword not in sql


def test_upgrade_contains_exactly_two_raw_sql_statements():
    """upgrade 里只有两条裸 SQL：建扩展和回填。

    这是一道闸门：任何新加的 UPDATE / DELETE / ALTER 都会让这条断言失败，
    逼着写迁移的人解释清楚自己往库里发了什么。
    """
    executes = [call for call in upgrade_operations() if call.startswith("op.execute(")]

    assert len(executes) == 2
    joined = " ".join(executes)
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in joined
    assert "SEARCH_TEXT_BACKFILL" in joined


def test_backfill_sources_the_document_title_section_title_and_content():
    """回填的三个片段：文档标题、小节标题、切片正文，按这个顺序拼接。"""
    sql = load_migration().SEARCH_TEXT_BACKFILL

    assert "kd.document_title" in sql
    assert "kc.section_title" in sql
    assert "kc.content" in sql
    assert sql.index("kd.document_title") < sql.index("kc.section_title") < sql.index("kc.content")


def test_backfill_joins_documents_on_the_foreign_key():
    sql = load_migration().SEARCH_TEXT_BACKFILL

    assert "FROM knowledge_documents AS kd" in sql
    assert "kd.id = kc.document_id" in sql


def test_backfill_uses_concat_ws_not_pipe_concatenation():
    """必须用 concat_ws。

    `||` 的语义是「有一个 NULL 结果就是 NULL」：小节标题一为空，
    整条 search_text 就变成 NULL，而这一列是 NOT NULL，回填会当场失败。
    concat_ws 会跳过 NULL 参数。
    """
    sql = load_migration().SEARCH_TEXT_BACKFILL

    assert "concat_ws(" in sql
    assert "||" not in sql


def test_backfill_only_updates_rows_that_are_still_empty():
    """带上 search_text = '' 的条件，重复执行才是安全的。"""
    sql = load_migration().SEARCH_TEXT_BACKFILL

    assert "kc.search_text = ''" in sql


def test_upgrade_creates_the_gin_trigram_index():
    creates = [call for call in upgrade_operations() if call.startswith("op.create_index(")]

    assert len(creates) == 1
    call = creates[0]
    assert "SEARCH_TEXT_TRGM_INDEX" in call
    assert "'knowledge_chunks'" in call
    assert "['search_text']" in call


def test_index_uses_gin_and_the_trigram_operator_class():
    """索引类型必须是 gin + gin_trgm_ops。

    建错成默认的 B-tree 也不会报错，只是「包含」类查询完全用不上它——
    静默失效比报错更难发现。
    """
    call = [c for c in upgrade_operations() if c.startswith("op.create_index(")][0]

    assert "postgresql_using='gin'" in call
    assert "postgresql_ops={'search_text': 'gin_trgm_ops'}" in call


def test_index_is_created_after_the_backfill():
    """先回填再建索引。

    反过来的话，回填的每一行都要额外维护一次索引，存量数据多时明显变慢。
    """
    operations = upgrade_operations()
    backfill_position = next(
        index for index, call in enumerate(operations) if "SEARCH_TEXT_BACKFILL" in call
    )
    index_position = next(
        index for index, call in enumerate(operations) if call.startswith("op.create_index(")
    )

    assert backfill_position < index_position


def test_index_name_matches_the_orm_constant():
    """迁移里的索引名与模型里的常量必须一致。

    不一致时，以后 autogenerate 会认为索引丢了，再生成一条重复建索引的迁移。
    """
    from app.models.knowledge import SEARCH_TEXT_TRGM_INDEX

    assert load_migration().SEARCH_TEXT_TRGM_INDEX == SEARCH_TEXT_TRGM_INDEX
    assert load_migration().SEARCH_TEXT_TRGM_INDEX == (
        "ix_knowledge_chunks_search_text_trgm"
    )


def test_migration_contains_no_f_string_sql():
    """SQL 不允许用 f-string 拼。

    一旦开了这个口子，将来「顺手」把某个变量插进去就没人拦得住了，
    而这条语句执行在回填阶段、出问题时已经写进库里了。
    """
    tree = ast.parse(MIGRATION_FILE.read_text(encoding="utf-8"))
    f_strings = [node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)]

    assert f_strings == []


def test_sql_constants_contain_no_template_placeholders():
    """SQL 常量里不该有花括号或占位符——它们只接受固定文本。"""
    module = load_migration()

    for sql in (module.SEARCH_TEXT_BACKFILL, module.EMPTY_JSONB_ARRAY):
        assert "{" not in sql
        assert "%s" not in sql


# --------------------------------------------------------------------------
# downgrade
# --------------------------------------------------------------------------


def test_downgrade_drops_the_index_before_the_columns():
    """顺序不能反。

    先删列的话，索引会被连带删掉，后面那条 drop_index 会因为
    「索引不存在」直接报错——回滚路径平时不跑，写错了要等到真回滚时才发现。
    """
    operations = downgrade_operations()
    index_position = next(
        index for index, call in enumerate(operations) if call.startswith("op.drop_index(")
    )
    column_positions = [
        index for index, call in enumerate(operations) if call.startswith("op.drop_column(")
    ]

    assert column_positions
    assert index_position < min(column_positions)


@pytest.mark.parametrize("column_name", METADATA_COLUMNS)
def test_downgrade_drops_each_metadata_column(column_name):
    assert any(
        call == f"op.drop_column('knowledge_chunks', '{column_name}')"
        for call in downgrade_operations()
    )


def test_downgrade_removes_only_what_this_migration_added():
    """回滚只该删掉自己建的东西：一个索引 + 三列，不删表。"""
    operations = downgrade_operations()

    assert len(operations) == 1 + len(METADATA_COLUMNS)
    assert operations[0].startswith("op.drop_index(")
    assert sum(call.startswith("op.drop_column(") for call in operations) == 3
    assert not any("drop_table" in call for call in operations)


def test_downgrade_does_not_drop_pg_trgm():
    """pg_trgm 是库级别的共享对象。

    将来别的表、别的迁移也会用它；回滚一个功能不该顺手废掉一个公共扩展——
    和上一个迁移回滚时不删 vector 是同一条理由。
    """
    joined = " ".join(downgrade_calls()).lower()

    assert "drop extension" not in joined
    assert "pg_trgm" not in joined


# --------------------------------------------------------------------------
# 数据安全：迁移不许碰的东西
# --------------------------------------------------------------------------


@pytest.mark.parametrize("function_name", ["upgrade", "downgrade"])
def test_migration_never_deletes_rows_or_tables(function_name):
    """没有 DELETE、没有 DROP TABLE、没有 TRUNCATE——
    文档数、切片数、非空向量数因此不可能变。"""
    joined = " ".join(ordered_calls(function_name)).upper()

    assert "DELETE" not in joined
    assert "TRUNCATE" not in joined
    assert joined.count("DROP_TABLE") == 0


def test_backfill_writes_only_search_text():
    """回填的 SET 子句只能有一个赋值目标：search_text。

    content 和 content_for_embedding 是回填的**来源**，只能出现在等号右边。
    这条断言同时挡住了改 updated_at 的风险——回填是纯 SQL，
    不经过 ORM，因此也不会触发 onupdate。
    """
    sql = load_migration().SEARCH_TEXT_BACKFILL
    set_clause = sql.split("SET", 1)[1].split("FROM", 1)[0]

    assert assignment_targets(set_clause) == ["search_text"]


def assignment_targets(clause: str) -> list[str]:
    """粗略取出 `列 = 值` 里等号左侧的列名。

    只看标识符；等号右侧可能带括号和逗号（concat_ws），所以不能简单按逗号切。
    """
    return [
        match.group(1)
        for match in re.finditer(r"([a-z_][a-z_0-9]*)\s*=", clause, flags=re.IGNORECASE)
    ]


@pytest.mark.parametrize("column_name", COLUMNS_THAT_MUST_NOT_BE_WRITTEN)
def test_backfill_does_not_write_any_other_column(column_name):
    sql = load_migration().SEARCH_TEXT_BACKFILL
    set_clause = sql.split("SET", 1)[1].split("FROM", 1)[0]

    assert column_name not in assignment_targets(set_clause)


def test_migration_never_updates_the_documents_table():
    """回填只读 knowledge_documents，不改它——
    文档表的 content_hash 和 chunk_count 必须原样不动。

    注意断言的是调用列表：迁移把 SQL 放在模块常量里，调用文本里
    根本不出现文档表名，所以「调用里不提 knowledge_documents」本身就是结论。
    """
    joined = " ".join(upgrade_calls() + downgrade_calls())
    sql = load_migration().SEARCH_TEXT_BACKFILL.upper()

    assert "UPDATE KNOWLEDGE_DOCUMENTS" not in sql
    assert "knowledge_documents" not in joined
    assert "alter_column" not in joined
    assert "update(" not in joined


def test_migration_does_not_touch_any_retail_table():
    joined = " ".join(upgrade_calls() + downgrade_calls())

    for table in RETAIL_TABLES:
        assert f"'{table}'" not in joined, f"迁移里提到了零售表 {table}"


# --------------------------------------------------------------------------
# 兼容性：空库与已有数据的库都要能跑
# --------------------------------------------------------------------------


@pytest.mark.parametrize("column_name", METADATA_COLUMNS)
def test_added_columns_carry_a_server_default_so_not_null_is_satisfiable(column_name):
    """三列都是 NOT NULL。

    没有 server default 的话，表里只要有存量数据，add_column 就会失败；
    有了默认值，空表和满表都能过——这正是「迁移支持空知识库」的根据。
    """
    call = add_column_call(column_name)

    assert "nullable=False" in call
    assert "server_default=" in call


@pytest.mark.parametrize("column_name", ["keywords", "aliases"])
def test_jsonb_columns_are_added_as_jsonb_with_an_array_default(column_name):
    """列类型必须是 JSONB，默认值必须解析成 JSON 数组。

    默认值写成不带 ::jsonb 的 '[]'，或者手滑写成 '"[]"'，
    库里存的就是 JSON 字符串；后续 jsonb_typeof(keywords) = 'array'
    之类的检查会静默漏掉这些行。
    """
    call = add_column_call(column_name)

    assert "postgresql.JSONB()" in call
    assert "EMPTY_JSONB_ARRAY" in call
    assert load_migration().EMPTY_JSONB_ARRAY == "'[]'::jsonb"


def test_search_text_is_added_as_text_with_an_empty_string_default():
    call = add_column_call("search_text")

    assert "sa.Text()" in call
    assert 'sa.text("\'\'")' in call


def test_backfill_is_a_noop_on_an_empty_knowledge_base():
    """空库上回填影响 0 行，也不会报错。

    一条 UPDATE ... FROM 在没有匹配行时就是空操作；语句里没有聚合、
    没有除零、没有依赖行数的地方，所以空库能安全跑过。
    真正在空库上执行一次由部署时的 `alembic upgrade head` 覆盖。
    """
    sql = load_migration().SEARCH_TEXT_BACKFILL

    assert "WHERE" in sql
    for risky in ("/", "AVG(", "SUM(", "COUNT("):
        assert risky not in sql
