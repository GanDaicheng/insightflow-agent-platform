"""Markdown 切片模块的单元测试。

**本文件不调用 embedding 接口、不连接数据库、不读取 .env、不发任何网络请求。**
切片模块本身就是纯函数：输入文本，输出数据结构。这里测的就是这件事。

测试数据分两类：
1. **合成 Markdown**（写到 tmp_path）——用来覆盖真实文档里不会出现的边界，
   比如「小节超过 1200 字符要二次切分」。真实文档最长的小节只有 694 字符，
   那条分支在真数据上永远走不到，只能靠合成数据覆盖，否则就是未验证的死代码。
2. **真实知识库文档**（backend/knowledge_seed/retail）——只断言不变量，
   不断言具体 chunk 数。文档内容会随项目演进，写死数字会让测试变成绊脚石。
"""

import ast
import hashlib
import pathlib
import sys
import textwrap

import pytest

from app.services import knowledge_chunking
from app.services.knowledge_chunking import (
    MAX_SECTION_CHARS,
    build_content_for_embedding,
    chunk_hash,
    clean_markdown_body,
    estimate_token_count,
    knowledge_document_paths,
    load_knowledge_chunks,
    normalize_section_title,
    parse_markdown_document,
    split_oversized_section,
)

KNOWLEDGE_SEED_DIR = pathlib.Path(__file__).resolve().parents[1] / "knowledge_seed"
REAL_DOCS_DIR = KNOWLEDGE_SEED_DIR / "retail"

REAL_DOCUMENT_NAMES = {
    "member_rules.md",
    "promotion_calendar.md",
    "regional_sales_rules.md",
    "retail_data_dictionary.md",
    "retail_metrics.md",
}


def write_doc(directory: pathlib.Path, name: str, text: str) -> pathlib.Path:
    """写一份合成文档。textwrap.dedent 让测试里的三引号块保持可读缩进。"""
    path = directory / name
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 标题识别与切片边界
# --------------------------------------------------------------------------


def test_h1_is_used_as_document_title(tmp_path):
    path = write_doc(
        tmp_path,
        "a.md",
        """
        # 零售核心指标口径说明

        ## 1. 销售额

        销售额取 net_amount。
        """,
    )

    chunks = parse_markdown_document(path)

    assert chunks
    assert {chunk.document_title for chunk in chunks} == {"零售核心指标口径说明"}


def test_missing_h1_falls_back_to_the_file_name(tmp_path):
    """没有一级标题时不报错：文件名本身已经是个可用标识，报错会卡住整批入库。"""
    path = write_doc(tmp_path, "no_title.md", "## 1. 只有二级标题\n\n正文。\n")

    chunks = parse_markdown_document(path)

    assert chunks[0].document_title == "no_title"


def test_each_h2_becomes_its_own_chunk_with_sequential_index(tmp_path):
    path = write_doc(
        tmp_path,
        "a.md",
        """
        # 指标口径

        ## 1. 销售额

        销售额正文。

        ## 2. 订单数

        订单数正文。

        ## 3. 客单价

        客单价正文。
        """,
    )

    chunks = parse_markdown_document(path)

    assert [chunk.section_title for chunk in chunks] == ["销售额", "订单数", "客单价"]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]
    assert all(chunk.source_file == "a.md" for chunk in chunks)


def test_h3_and_deeper_stay_inside_the_chunk(tmp_path):
    """三级标题留在正文里，不单独切片。"""
    path = write_doc(
        tmp_path,
        "a.md",
        """
        # 会员规则

        ## 1. 会员分析

        前言。

        ### 1.1 注意事项

        注意事项正文。

        #### 1.1.1 细节

        细节正文。
        """,
    )

    chunks = parse_markdown_document(path)

    assert len(chunks) == 1
    assert chunks[0].section_title == "会员分析"
    assert "### 1.1 注意事项" in chunks[0].content
    assert "#### 1.1.1 细节" in chunks[0].content


def test_empty_section_produces_no_chunk(tmp_path):
    path = write_doc(
        tmp_path,
        "a.md",
        """
        # 指标口径

        ## 1. 有内容的小节

        这里是正文。

        ## 2. 空小节

        ## 3. 只有空行的小节



        ## 4. 也有内容

        正文。
        """,
    )

    chunks = parse_markdown_document(path)

    assert [chunk.section_title for chunk in chunks] == ["有内容的小节", "也有内容"]


def test_front_matter_becomes_its_own_chunk(tmp_path):
    """H1 之后、第一个 H2 之前的内容是文档元信息，单独成一个 chunk，不静默丢弃。"""
    path = write_doc(
        tmp_path,
        "a.md",
        """
        # 零售核心指标口径说明

        > 文档类型：数据口径规范
        > 使用对象：经营分析

        ## 1. 销售额

        正文。
        """,
    )

    chunks = parse_markdown_document(path)

    assert [chunk.section_title for chunk in chunks] == ["文档元信息", "销售额"]
    assert "文档类型：数据口径规范" in chunks[0].content


def test_h1_immediately_followed_by_h2_yields_no_front_matter_chunk(tmp_path):
    path = write_doc(tmp_path, "a.md", "# 标题\n\n## 1. 小节\n\n正文。\n")

    chunks = parse_markdown_document(path)

    assert [chunk.section_title for chunk in chunks] == ["小节"]


# --------------------------------------------------------------------------
# content_for_embedding 与 hash
# --------------------------------------------------------------------------


def test_content_for_embedding_carries_document_and_section_title(tmp_path):
    path = write_doc(tmp_path, "a.md", "# 指标口径\n\n## 4. 客单价\n\n客单价 = 销售额 / 订单数。\n")

    chunk = parse_markdown_document(path)[0]

    assert "文档：指标口径" in chunk.content_for_embedding
    assert "小节：客单价" in chunk.content_for_embedding
    assert chunk.content in chunk.content_for_embedding
    # content 本身不含标题行，标题只存在于独立字段和 embedding 文本里
    assert not chunk.content.startswith("##")


def test_content_hash_is_sha256_of_the_embedding_text(tmp_path):
    path = write_doc(tmp_path, "a.md", "# 标题\n\n## 1. 小节\n\n正文。\n")

    chunk = parse_markdown_document(path)[0]

    expected = hashlib.sha256(chunk.content_for_embedding.encode("utf-8")).hexdigest()
    assert chunk.content_hash == expected
    assert len(chunk.content_hash) == 64


def test_content_hash_is_stable_across_repeated_parses(tmp_path):
    path = write_doc(tmp_path, "a.md", "# 标题\n\n## 1. 小节\n\n正文。\n")

    first = parse_markdown_document(path)
    second = parse_markdown_document(path)

    assert [chunk.content_hash for chunk in first] == [chunk.content_hash for chunk in second]


def test_hash_changes_when_only_the_section_title_changes():
    """这正是 hash 取 content_for_embedding 而不是 content 的原因。

    只改标题、正文一字未动时，算出来的向量其实变了（标题进了 embedding 文本），
    所以 hash 必须跟着变，否则入库脚本会以为它没变、跳过重算，向量就悄悄失真了。
    """
    same_body = "客单价 = 销售额 / 订单数。"

    before = chunk_hash(build_content_for_embedding("指标口径", "客单价", same_body))
    after = chunk_hash(build_content_for_embedding("指标口径", "笔单价", same_body))

    assert before != after


# --------------------------------------------------------------------------
# 清洗：不能伤到业务关键符号
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "orders.net_amount",
        "COUNT(DISTINCT orders.order_no)",
        "客单价 = 销售额 / 订单数",
        "`customers.member_level`",
    ],
)
def test_business_symbols_survive_cleaning(symbol, tmp_path):
    path = write_doc(tmp_path, "a.md", f"# 标题\n\n## 1. 小节\n\n口径：{symbol}\n")

    chunk = parse_markdown_document(path)[0]

    assert symbol in chunk.content


def test_list_and_table_content_is_preserved(tmp_path):
    path = write_doc(
        tmp_path,
        "a.md",
        """
        # 数据字典

        ## 1. orders 表

        | 字段 | 含义 |
        | --- | --- |
        | `net_amount` | 实付金额 |
        | `order_no` | 订单号 |

        - 来源表：`orders`
        - 计算逻辑：`SUM(orders.net_amount)`
        """,
    )

    content = parse_markdown_document(path)[0].content

    assert "| 字段 | 含义 |" in content
    assert "| `net_amount` | 实付金额 |" in content
    assert "- 来源表：`orders`" in content


def test_clean_markdown_body_collapses_blank_lines_and_trims_edges():
    raw = "  第一行   \n\n\n\n第二行\n   \n"

    cleaned = clean_markdown_body(raw)

    assert cleaned == "第一行\n\n第二行"


def test_clean_markdown_body_keeps_leading_indentation_for_nested_lists():
    """行首缩进表达列表层级，不能像行尾空白那样删掉。"""
    raw = "- 一级\n  - 二级\n    - 三级\n"

    assert clean_markdown_body(raw) == "- 一级\n  - 二级\n    - 三级"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4. 客单价", "客单价"),
        ("2. 11 月促销季", "11 月促销季"),
        ("9、会员分析注意事项", "会员分析注意事项"),
        ("订单数", "订单数"),
        ("  7. 区域销售额  ", "区域销售额"),
    ],
)
def test_section_number_prefix_is_stripped(raw, expected):
    assert normalize_section_title(raw) == expected


# --------------------------------------------------------------------------
# 超长小节二次切分
# --------------------------------------------------------------------------


def test_short_section_is_not_split():
    content = "短正文。"

    assert split_oversized_section(content) == [content]


def test_oversized_section_is_split_into_multiple_pieces():
    paragraph = "这是一段用于测试的正文内容。" * 20  # 约 280 字符
    content = "\n\n".join([paragraph] * 8)  # 远超上限

    assert len(content) > MAX_SECTION_CHARS

    pieces = split_oversized_section(content)

    assert len(pieces) > 1
    # 不丢内容：拼回去应当与原文等价（只是块之间的空行被规范化）
    assert "".join(pieces).replace("\n", "") == content.replace("\n", "")


def test_oversized_section_keeps_each_piece_within_the_limit_when_possible():
    paragraph = "这是一段用于测试的正文内容。" * 10  # 约 140 字符
    content = "\n\n".join([paragraph] * 20)

    pieces = split_oversized_section(content)

    assert all(len(piece) <= MAX_SECTION_CHARS for piece in pieces)


def test_an_indivisible_block_is_never_cut_in_half():
    """单个块本身就超限时，宁可让它超长，也不从中间切开。

    Markdown 表格和列表常常是一整块没有空行，从中间断开会把表头和表体分家，
    那比切片超长更糟——超长只是多花 token，断掉的表格则完全没法用。
    """
    one_huge_block = "无空行的超长块。" * 200

    pieces = split_oversized_section(one_huge_block)

    assert pieces == [one_huge_block]
    assert len(pieces[0]) > MAX_SECTION_CHARS


def test_table_stays_whole_inside_a_single_piece():
    table_rows = "\n".join(
        f"| `col_{index}` | 第 {index} 行的说明文字，用来把表格撑长一些 |" for index in range(40)
    )
    table = "| 字段 | 含义 |\n| --- | --- |\n" + table_rows
    filler = "填充段落。" * 30
    content = "\n\n".join([filler] * 5 + [table] + [filler] * 5)

    assert len(content) > MAX_SECTION_CHARS

    pieces = split_oversized_section(content)

    holders = [piece for piece in pieces if "| 字段 | 含义 |" in piece]
    assert len(holders) == 1, "表格被拆到了多个片段里"
    assert table in holders[0], "表格没有被完整保留在同一个片段中"


def test_parse_splits_an_oversized_document_section(tmp_path):
    paragraph = "这是一段用于测试的正文内容。" * 15
    body = "\n\n".join([paragraph] * 10)
    path = write_doc(tmp_path, "long.md", f"# 长文档\n\n## 1. 超长小节\n\n{body}\n")

    chunks = parse_markdown_document(path)

    assert len(chunks) > 1
    assert {chunk.section_title for chunk in chunks} == {"超长小节"}
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    # 拆出来的每一片都要能单独检索：标题上下文必须都带上
    assert all("小节：超长小节" in chunk.content_for_embedding for chunk in chunks)


# --------------------------------------------------------------------------
# 目录级加载
# --------------------------------------------------------------------------


def test_load_knowledge_chunks_is_sorted_by_file_name(tmp_path):
    write_doc(tmp_path, "c.md", "# C\n\n## 1. 小节\n\n正文。\n")
    write_doc(tmp_path, "a.md", "# A\n\n## 1. 小节\n\n正文。\n")
    write_doc(tmp_path, "b.md", "# B\n\n## 1. 小节\n\n正文。\n")

    chunks = load_knowledge_chunks(tmp_path)

    assert [chunk.source_file for chunk in chunks] == ["a.md", "b.md", "c.md"]


def test_chunk_index_restarts_for_each_document(tmp_path):
    write_doc(tmp_path, "a.md", "# A\n\n## 1. 一\n\n正文。\n\n## 2. 二\n\n正文。\n")
    write_doc(tmp_path, "b.md", "# B\n\n## 1. 一\n\n正文。\n")

    chunks = load_knowledge_chunks(tmp_path)
    by_file: dict[str, list[int]] = {}
    for chunk in chunks:
        by_file.setdefault(chunk.source_file, []).append(chunk.chunk_index)

    assert by_file == {"a.md": [0, 1], "b.md": [0]}


def test_load_knowledge_chunks_ignores_non_markdown_files(tmp_path):
    write_doc(tmp_path, "a.md", "# A\n\n## 1. 小节\n\n正文。\n")
    (tmp_path / "notes.txt").write_text("不该被读到", encoding="utf-8")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")

    chunks = load_knowledge_chunks(tmp_path)

    assert {chunk.source_file for chunk in chunks} == {"a.md"}


# --------------------------------------------------------------------------
# 递归扫描：知识库按领域分了子目录
# --------------------------------------------------------------------------


def test_documents_in_subdirectories_are_picked_up(tmp_path):
    """知识种子目录允许按业务主题拆分子目录，扫描必须保持递归。"""
    write_doc(tmp_path, "top.md", "# T\n\n## 1. 小节\n\n正文。\n")
    nested = tmp_path / "领域a"
    nested.mkdir()
    write_doc(nested, "nested.md", "# N\n\n## 1. 小节\n\n正文。\n")

    chunks = load_knowledge_chunks(tmp_path)

    assert {chunk.source_file for chunk in chunks} == {"top.md", "nested.md"}


def test_recursive_scan_order_is_deterministic_by_relative_path(tmp_path):
    """排序键必须是相对路径，不能是文件名。

    顺序抖动会让入库脚本每次都判定成「文档变了」——它的幂等判断建立在
    切片内容 hash 之上，而 hash 是按顺序拼出来的。
    """
    first = tmp_path / "a领域"
    second = tmp_path / "b领域"
    first.mkdir()
    second.mkdir()
    write_doc(second, "same.md", "# B\n\n## 1. 小节\n\n正文。\n")
    write_doc(first, "same.md", "# A\n\n## 1. 小节\n\n正文。\n")

    paths = knowledge_document_paths(tmp_path)

    assert [path.parent.name for path in paths] == ["a领域", "b领域"]
    # 跑两次结果必须完全一致
    assert knowledge_document_paths(tmp_path) == paths


def test_flat_directory_order_is_unchanged_by_the_recursive_scan(tmp_path):
    """只有一层文件时，递归扫描的结果必须与原来的按文件名排序完全一致。

    这是回归保护：改成递归不能改变知识库原有的切片顺序，
    否则所有既有文档的 hash 都会变，下一次入库会整篇重写。
    """
    write_doc(tmp_path, "c.md", "# C\n\n## 1. 小节\n\n正文。\n")
    write_doc(tmp_path, "a.md", "# A\n\n## 1. 小节\n\n正文。\n")
    write_doc(tmp_path, "b.md", "# B\n\n## 1. 小节\n\n正文。\n")

    names = [path.name for path in knowledge_document_paths(tmp_path)]

    assert names == ["a.md", "b.md", "c.md"]


def test_missing_directory_yields_no_documents(tmp_path):
    """目录不存在时返回空列表，而不是抛异常。

    扫描函数是纯的、不负责报错；「目录不存在」由脚本层判断并给出提示。
    """
    assert knowledge_document_paths(tmp_path / "不存在") == []


def test_real_knowledge_base_has_no_duplicate_file_names():
    """**跨目录的文件名不能重复。**

    文档在数据库里以 `source_file`（也就是纯文件名）为键。
    两个不同领域各有一份 `metrics.md` 的话，后入库的那份会覆盖前一份，
    而且两边的切片会混在同一个文档下——内容看起来还在，来源却错了。
    这条断言让「加文档时撞名」在测试里就暴露，而不是等到检索结果开始串味。
    """
    names = [path.name for path in knowledge_document_paths(KNOWLEDGE_SEED_DIR)]
    duplicates = sorted({name for name in names if names.count(name) > 1})

    assert not duplicates, f"知识库里出现了重名文档：{duplicates}"


def test_real_knowledge_base_covers_retail_knowledge():
    """当前公开知识库只服务电商经营分析主线。"""
    by_domain: dict[str, int] = {}
    for path in knowledge_document_paths(KNOWLEDGE_SEED_DIR):
        by_domain[path.parent.name] = by_domain.get(path.parent.name, 0) + 1

    assert by_domain.get("retail", 0) >= 5
    assert set(by_domain) <= {"retail"}


# --------------------------------------------------------------------------
# 真实知识库文档：只断言不变量，不锁死具体数量
# --------------------------------------------------------------------------


def test_real_documents_all_produce_chunks():
    chunks = load_knowledge_chunks(REAL_DOCS_DIR)

    assert {chunk.source_file for chunk in chunks} == REAL_DOCUMENT_NAMES


def test_real_documents_have_no_empty_chunks_and_contiguous_indexes():
    chunks = load_knowledge_chunks(REAL_DOCS_DIR)

    positions: dict[str, list[int]] = {}
    for chunk in chunks:
        assert chunk.content.strip(), f"{chunk.source_file} #{chunk.chunk_index} 正文为空"
        assert chunk.section_title.strip()
        assert chunk.char_count == len(chunk.content)
        assert chunk.estimated_token_count > 0
        positions.setdefault(chunk.source_file, []).append(chunk.chunk_index)

    for name, indexes in positions.items():
        assert indexes == list(range(len(indexes))), f"{name} 的 chunk_index 不连续"


def test_real_documents_are_small_enough_for_direct_retrieval():
    """当前 5 个文档都远低于切分阈值，属于「一个小节一个切片」的理想状态。

    这条断言是给未来的自己看的：哪天有人往知识库里塞了一份超长文档导致大量二次切分，
    这里会失败，提醒切片策略该重新评估了。
    """
    chunks = load_knowledge_chunks(REAL_DOCS_DIR)

    oversized = [chunk for chunk in chunks if chunk.char_count > MAX_SECTION_CHARS]
    assert not oversized, f"出现超长切片：{[(c.source_file, c.section_title) for c in oversized]}"


# --------------------------------------------------------------------------
# 依赖边界：只用标准库，不碰 .env / 数据库 / embedding
# --------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path) -> set[str]:
    """收集一个 Python 文件里所有 import 到的模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


# 切片模块允许依赖的项目内模块。**必须都是纯模块**（无 I/O、无外部系统）。
#
# 写成显式白名单而不是「随便 import 什么都行」：多加一个就要在这里登记一次，
# 逼着人想清楚「它是不是纯的」。下面那组 forbidden 参数化测试继续逐条挡着
# openai / sqlalchemy / 配置 这些真正危险的东西。
ALLOWED_PROJECT_MODULES = ("app.services.document_normalization",)


def test_chunking_module_only_depends_on_stdlib_and_pure_project_modules():
    """切片模块必须保持纯：不该够得着网络、数据库、配置或任何外部系统。

    这条原来写的是「只 import 标准库」。加入「从内容切片」之后，它需要复用
    document_normalization 里的 BOM / 换行归一与标题兜底——那是个纯模块
    （只 import pathlib 和 AppError），没有违反这条不变量的**本意**。
    所以从「只准标准库」放宽成「标准库 + 显式登记的项目内纯模块」。

    比较用前缀而不是全等：_imported_modules 会把 `from x import Y` 记成
    两条（`x` 和 `x.Y`），只比全等的话 `x.Y` 会被误判成越界。
    """
    modules = _imported_modules(pathlib.Path(knowledge_chunking.__file__))

    unexpected = {
        module
        for module in modules
        if module.split(".")[0] not in sys.stdlib_module_names
        and not module.startswith(ALLOWED_PROJECT_MODULES)
    }
    assert not unexpected, f"切片模块引入了未登记的依赖：{sorted(unexpected)}"


@pytest.mark.parametrize(
    "forbidden_prefix",
    [
        "openai",  # 不许调用 embedding 接口
        "sqlalchemy",  # 不许连数据库
        "asyncpg",
        "app.core.config",  # 不许读 .env
        "app.repositories",
        "dotenv",
        "requests",
        "httpx",
    ],
)
def test_chunking_module_does_not_reach_for_external_systems(forbidden_prefix):
    modules = _imported_modules(pathlib.Path(knowledge_chunking.__file__))

    offenders = [module for module in modules if module.startswith(forbidden_prefix)]
    assert not offenders, f"切片模块不该依赖 {forbidden_prefix}：{offenders}"


def test_estimate_token_count_grows_with_text_length():
    short = estimate_token_count("客单价")
    long = estimate_token_count("客单价" * 50)

    assert 0 < short < long
