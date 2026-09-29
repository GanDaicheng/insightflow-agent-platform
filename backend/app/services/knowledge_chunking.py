"""Markdown 知识文档切片：把知识库文档切成可检索、可入库的 chunk。

切法（与 backend/knowledge_seed/retail/ 下的 5 个文档结构对齐）：

- `#` 一级标题 → document_title，不产生正文
- `##` 二级标题 → 一个 chunk 的主边界
- `###` 及更深 → 留在 chunk 正文里，不单独切
- H1 与第一个 H2 之间的内容（文档头部的引用块）→ 单独一个 chunk，
  section_title 用「文档元信息」。**不静默丢弃**：那几句写的是文档适用范围和使用对象，
  丢了以后「这份口径文档是给谁看的」这类问题就永远答不出来。
  想排除它，只需把 _FRONT_MATTER_SECTION_TITLE 的处理去掉一处。

为什么按二级标题切，而不是按固定字数切？
这些文档的小节是**语义完整**的最小单元——「客单价」那一节自成一段完整口径说明。
按固定字数硬切会在「客单价 = 销售额 / 订单数」这种关键句中间断开，
检索时两边都拿不到完整口径，等于把文档切坏了。字数只作为**兜底**：
小节超过 MAX_SECTION_CHARS 时才按段落二次切分。

清洗的边界：只做「不影响语义」的整理——去行尾空白、压缩连续空行、去首尾空白。
**绝不改动行内内容**，所以 `orders.net_amount`、`COUNT(DISTINCT orders.order_no)`、
`客单价 = 销售额 / 订单数` 这些关键符号原样保留。

二次切分的最小单位是「空行分隔的块」，块内部不再切：
Markdown 表格和列表经常是一个没有空行的连续块，从中间切断会把表头和表体分开，
那比 chunk 超长更糟。所以一个超长块宁可整体超限，也不从中间断。

本模块只用标准库：不读 .env、不连数据库、不调用 embedding 接口、不依赖任何第三方包。
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.services.document_normalization import (
    MARKDOWN_FILE_TYPE,
    PLAIN_TEXT_FILE_TYPE,
    title_from_source_file,
)

# 小节超过这个字符数才按段落二次切分。当前 5 个文档最长的小节约 715 字符，
# 所以这个分支现在不会触发——它是为以后更长的文档准备的，测试用合成文档覆盖。
MAX_SECTION_CHARS = 1200

# 文档头部（H1 之后、第一个 H2 之前）那个引用块的 section_title
_FRONT_MATTER_SECTION_TITLE = "文档元信息"

# 纯文本文件没有内部结构，整篇算一个小节，用这个固定的标题。
PLAIN_TEXT_SECTION_TITLE = "正文"

_H1_PATTERN = re.compile(r"^#\s+(?P<title>.*\S)\s*$")
_H2_PATTERN = re.compile(r"^##\s+(?P<title>.*\S)\s*$")
_ANY_HEADING_PATTERN = re.compile(r"^#{1,6}\s+")
# 只剥掉小节编号（"1. " / "2、"），"11 月促销季" 里的 11 不受影响
_SECTION_NUMBER_PATTERN = re.compile(r"^\d+\s*[.、]\s*")

_CJK_PATTERN = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class KnowledgeChunk:
    """一个待入库的切片。

    content 与 content_for_embedding 是两个不同的东西，别混用：
    - content 是要展示、要给人看的正文，不含标题（标题单独存字段）；
    - content_for_embedding 是要送去算向量的文本，带上标题上下文，
      因为脱离标题的「它 = 销售额 / 订单数」是检索不出来的。
    """

    source_file: str
    document_title: str
    section_title: str
    chunk_index: int
    content: str
    content_for_embedding: str
    content_hash: str
    estimated_token_count: int
    char_count: int


def normalize_section_title(raw_title: str) -> str:
    """去掉小节标题的编号前缀。

    "4. 客单价" → "客单价"；"2. 11 月促销季" → "11 月促销季"（只剥一层编号）。
    编号代表的是文档内顺序，而顺序已经由 chunk_index 表达了，留着只是噪音。
    """
    return _SECTION_NUMBER_PATTERN.sub("", raw_title.strip()).strip() or raw_title.strip()


def clean_markdown_body(text: str) -> str:
    """基础清洗：只做不改变语义的整理。

    行尾空白对 Markdown 渲染没有意义，但会让 hash 随编辑器不同而变化，
    所以统一去掉。行首缩进要保留——嵌套列表靠它表达层级。
    """
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = "\n".join(lines)
    # 三个以上连续换行压成一个空行；正文不需要靠空行表达任何含义
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def estimate_token_count(text: str) -> int:
    """粗略估算 token 数。

    中文按「一字约一 token」，英文/数字按「一个词约 1.3 token」。
    这是有意保守的估算：真实分词器对中文常常 1 字不到 1 token，
    高估一点比低估安全（高估只会让切片显得偏长，低估会让人误以为还能再塞内容）。
    """
    cjk_count = len(_CJK_PATTERN.findall(text))
    non_cjk = _CJK_PATTERN.sub(" ", text)
    word_count = len(re.findall(r"[A-Za-z0-9_]+", non_cjk))
    return cjk_count + round(word_count * 1.3)


def build_content_for_embedding(document_title: str, section_title: str, content: str) -> str:
    """拼出送去算向量的文本：标题上下文 + 正文。

    多这两行标题，检索命中率差别很大——「客单价怎么算」这类问题，
    字面上跟正文里的「销售额 / 订单数」并不像，但跟标题很像。
    """
    return f"文档：{document_title}\n小节：{section_title}\n\n{content}"


def chunk_hash(content_for_embedding: str) -> str:
    """切片内容的稳定 hash，用于判断入库时是否需要重新算向量。

    刻意对 content_for_embedding 而不是 content 取 hash：只要任何会影响向量的东西变了
    （正文改了、文档标题改了、小节标题改了），hash 就该变。
    只对 content 取 hash 的话，改标题时 hash 不变，向量就成了旧标题算出来的，静默失真。
    """
    return hashlib.sha256(content_for_embedding.encode("utf-8")).hexdigest()


def split_oversized_section(content: str, limit: int = MAX_SECTION_CHARS) -> list[str]:
    """把超长小节按「空行分隔的块」分组，尽量不超限。

    块内部绝不切开——Markdown 表格和列表常常是一整块，从中间断开会把表头和表体分家。
    因此如果单个块本身就超过 limit，它会被原样作为一个（超长的）片段返回。
    超长比切坏好：检索到一个长切片只会多花点 token，检索到一个断掉的表格则完全没用。

    第一版不做 overlap：overlap 会让同一段文字在多个切片里重复，
    入库后表现为多条高度相似的召回结果。等真的观察到边界丢信息再加不迟。
    """
    if len(content) <= limit:
        return [content]

    blocks = [block for block in re.split(r"\n\s*\n", content) if block.strip()]

    pieces: list[str] = []
    current: list[str] = []
    current_length = 0

    for block in blocks:
        # 再加一块就超限时先收口，把当前累积的块作为一个片段
        if current and current_length + len(block) + 2 > limit:
            pieces.append("\n\n".join(current))
            current = []
            current_length = 0
        current.append(block)
        current_length += len(block) + 2

    if current:
        pieces.append("\n\n".join(current))

    return [piece for piece in pieces if piece.strip()]


def _iter_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """按二级标题把行切成 (小节标题, 小节正文行)。

    H1 之后、第一个 H2 之前的行归入一个标题为空串的「头部小节」。
    调用方据此决定是当作文档元信息还是丢弃。
    """
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in lines:
        h2 = _H2_PATTERN.match(line)
        if h2:
            sections.append((current_title or "", current_lines))
            current_title = h2.group("title")
            current_lines = []
            continue
        current_lines.append(line)

    sections.append((current_title or "", current_lines))
    return sections


def parse_markdown_text(
    text: str, *, source_file: str, title: str | None = None
) -> list[KnowledgeChunk]:
    """解析 Markdown 文本，返回切片列表（chunk_index 从 0 递增）。

    **从文本切片**，而不是从路径——这是「支持上传内容入库」的关键一步：
    上传来的内容本来就没有磁盘路径，硬要先落盘再读既多余又引入临时文件。

    title 的优先级（三者依次兜底）：
      1. 调用方显式传的 title（比如上传表单里填的标题）；
      2. 正文里的一级标题；
      3. 文件名去掉后缀。
    没有一级标题时不报错——报错会让一份格式不规范的文档卡住整批入库，
    而文件名本身已经是个可用的标识。
    """
    lines = text.splitlines()

    document_title = ""
    body_start = 0
    for position, line in enumerate(lines):
        h1 = _H1_PATTERN.match(line)
        # 只认第一个 H1 当标题；后续再出现的 H1 按普通正文处理
        if h1 and not document_title:
            document_title = h1.group("title")
            body_start = position + 1
            break

    # 调用方给的标题优先于文件里的一级标题：它是「这次入库想叫它什么」，
    # 比文档自己写什么更权威（重命名一份旧文档时尤其明显）。
    #
    # 注意 body_start 仍然按「找到的那一行 H1」算：即使标题被调用方覆盖，
    # 正文里那一行 H1 依然是标题行，不该混进切片正文。
    document_title = title or document_title or title_from_source_file(source_file)

    chunks: list[KnowledgeChunk] = []
    chunk_index = 0

    for raw_title, section_lines in _iter_sections(lines[body_start:]):
        if raw_title:
            section_title = normalize_section_title(raw_title)
        else:
            # 头部元信息；正文为空时（例如 H1 紧接 H2）整段跳过
            section_title = _FRONT_MATTER_SECTION_TITLE

        body = clean_markdown_body("\n".join(section_lines))
        if not body:
            continue  # 空小节不产生 chunk

        for piece in split_oversized_section(body):
            content_for_embedding = build_content_for_embedding(
                document_title, section_title, piece
            )
            chunks.append(
                KnowledgeChunk(
                    source_file=source_file,
                    document_title=document_title,
                    section_title=section_title,
                    chunk_index=chunk_index,
                    content=piece,
                    content_for_embedding=content_for_embedding,
                    content_hash=chunk_hash(content_for_embedding),
                    estimated_token_count=estimate_token_count(piece),
                    char_count=len(piece),
                )
            )
            chunk_index += 1

    return chunks


def parse_plain_text(
    text: str, *, source_file: str, title: str | None = None
) -> list[KnowledgeChunk]:
    """解析纯文本，返回切片列表。

    纯文本没有标题层级，所以**整篇算一个小节**：section_title 固定为「正文」。
    过长时由 split_oversized_section 按空行分段切开（同一个函数，
    和 Markdown 共用——分段的规则不该因为文件类型而不同）。

    为什么不拿文件名当小节标题？那会拼出「文档：订单规范 / 小节：订单规范」
    这样重复的上下文。小节名要回答的是「这一段在讲什么」，
    对一个没有内部结构的文件，诚实的答案就是「正文」。
    """
    document_title = title or title_from_source_file(source_file)
    body = clean_markdown_body(text)
    if not body:
        return []

    chunks: list[KnowledgeChunk] = []
    for chunk_index, piece in enumerate(split_oversized_section(body)):
        content_for_embedding = build_content_for_embedding(
            document_title, PLAIN_TEXT_SECTION_TITLE, piece
        )
        chunks.append(
            KnowledgeChunk(
                source_file=source_file,
                document_title=document_title,
                section_title=PLAIN_TEXT_SECTION_TITLE,
                chunk_index=chunk_index,
                content=piece,
                content_for_embedding=content_for_embedding,
                content_hash=chunk_hash(content_for_embedding),
                estimated_token_count=estimate_token_count(piece),
                char_count=len(piece),
            )
        )
    return chunks


def parse_document_text(
    text: str,
    *,
    source_file: str,
    file_type: str,
    title: str | None = None,
) -> list[KnowledgeChunk]:
    """按文件类型选切片策略。file_type 应当已经过 normalize_file_type 归一。

    分发放在这里而不是调用方：调用方只需要知道「这是一份 md/txt」，
    不该同时知道「md 该怎么切」。将来加新类型（比如 csv）时，
    只在这里多一个分支，入库服务一行都不用改。
    """
    if file_type == MARKDOWN_FILE_TYPE:
        return parse_markdown_text(text, source_file=source_file, title=title)
    if file_type == PLAIN_TEXT_FILE_TYPE:
        return parse_plain_text(text, source_file=source_file, title=title)
    # 理论上到不了：normalize_file_type 已经把不支持的类型挡在门外。
    # 保留这一支是为了「传进来没归一过的类型」时能立刻炸，而不是静默返回空列表。
    raise ValueError(f"没有对应的切片策略：{file_type!r}")


def parse_markdown_document(path: Path) -> list[KnowledgeChunk]:
    """解析一个 Markdown **文件**，返回它的切片列表。

    行为与重构前完全一致（source_file 取文件名、标题退回文件名去后缀）——
    它只是 parse_markdown_text 的一层薄封装，让目录扫描那条老路径继续可用。
    """
    return parse_markdown_text(
        path.read_text(encoding="utf-8"), source_file=path.name
    )


def knowledge_document_paths(directory: Path) -> list[Path]:
    """列出目录下（含子目录）的 Markdown 文件，按相对路径排序。

    为什么改成递归？知识库按业务主题分目录，而入库脚本只接收一个目录参数。
    递归扫描让「知识种子目录」保持整体，同时允许后续新增库存、履约或售后主题。

    排序键用**相对路径**而不是文件名：文件名相同、目录不同的两份文档
    （比如两个领域各有一份 `metrics.md`）在只按文件名排时顺序不确定，
    而切片顺序必须稳定——入库脚本的幂等判断建立在内容 hash 之上，
    顺序抖动会让每次运行都判定成「文档变了」。
    对只有一层文件的目录，相对路径排序与按文件名排序结果完全一致，
    所以既有行为没有变化。
    """
    if not directory.is_dir():
        return []

    def sort_key(path: Path) -> str:
        try:
            return path.relative_to(directory).as_posix()
        except ValueError:  # pragma: no cover - rglob 的结果一定在 directory 下
            return path.name

    return sorted(
        (path for path in directory.rglob("*.md") if path.is_file()),
        key=sort_key,
    )


def load_knowledge_chunks(directory: Path) -> list[KnowledgeChunk]:
    """把一个目录下所有 Markdown 文档切成切片，按文件名顺序拼接返回。"""
    chunks: list[KnowledgeChunk] = []
    for path in knowledge_document_paths(directory):
        chunks.extend(parse_markdown_document(path))
    return chunks


def summarize_chunks(chunks: Iterable[KnowledgeChunk]) -> dict[str, dict[str, int]]:
    """按文件汇总 chunk 数，供预览脚本打印。"""
    summary: dict[str, dict[str, int]] = {}
    for chunk in chunks:
        entry = summary.setdefault(chunk.source_file, {"chunks": 0, "chars": 0})
        entry["chunks"] += 1
        entry["chars"] += chunk.char_count
    return summary
