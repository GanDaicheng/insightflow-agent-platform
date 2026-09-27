"""Demo 模式的知识问答能力。

## 答案正文不写死在代码里

每条知识场景只登记「哪份文档的哪一节」（见 scenarios.py），正文在**运行时**
从 `backend/knowledge_seed/` 读出来。这样文档一改，demo 的答案自动跟着改，
不会出现「代码里一套说法、文档里另一套说法」——那种不一致最难发现，
因为两边看起来都像是对的。

## 不碰向量库、不碰模型

真实链路是「切片 → embedding → pgvector → 混合召回 → RRF → 精排 → 模型作答」。
demo 链路只用第一步的产物（Markdown 原文）做关键词定位，后面全部跳过。
所以它不需要 EMBEDDING_API_KEY、RERANK_API_KEY、OPENAI_API_KEY 中的任何一个，
也不需要数据库里已经有知识切片。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.core.paths import BACKEND_DIR
from app.demo.scenarios import (
    KNOWLEDGE_SCENARIOS,
    KnowledgeScenario,
    match_knowledge,
    supported_questions,
)
from app.services.rag_answer import (
    RagAnswer,
    RagRetrievalSummary,
    RagSource,
    build_preview,
)

KNOWLEDGE_SEED_DIR = BACKEND_DIR / "knowledge_seed"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class _Section:
    __slots__ = ("level", "title", "body")

    def __init__(self, level: int, title: str, body: list[str]) -> None:
        self.level = level
        self.title = title
        self.body = body


@lru_cache(maxsize=64)
def _read_sections(source_file: str) -> tuple[_Section, ...]:
    """把一份 Markdown 按标题切成小节。读不到就返回空元组。

    缓存是因为一次 demo 演示里同一份文档会被反复读，而文件在进程生命周期内
    不会变——真变了也该重启进程，而不是每次请求都去戳磁盘。
    """
    path = KNOWLEDGE_SEED_DIR / source_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()

    sections: list[_Section] = []
    current: _Section | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            current = _Section(len(match.group(1)), match.group(2).strip(), [])
            sections.append(current)
        elif current is not None:
            current.body.append(line)
    return tuple(sections)


def _extract(source_file: str, section_keyword: str) -> tuple[str, str] | None:
    """定位包含关键词的小节，返回 (小节标题, 小节正文)。

    取到下一个**层级不深于它**的标题为止——这样 H2 会把它的 H3 子节一起带上，
    而不会把下一节的内容也吞进来。
    """
    sections = _read_sections(source_file)
    for index, section in enumerate(sections):
        if section_keyword not in section.title:
            continue
        collected: list[str] = []
        for following in sections[index + 1 :]:
            if following.level <= section.level:
                break
            collected.append(f"{'#' * following.level} {following.title}")
            collected.extend(following.body)
        body = "\n".join(section.body + collected).strip()
        return section.title, body
    return None


def _document_title(source_file: str) -> str:
    """用文档的一级标题作标题；没有就退回文件名。"""
    for section in _read_sections(source_file):
        if section.level == 1:
            return section.title
    return Path(source_file).stem


def _answer_for(scenario: KnowledgeScenario) -> RagAnswer | None:
    found = _extract(scenario.source_file, scenario.section_keyword)
    if found is None:
        # 文档改了、小节标题对不上了。返回 None 让调用方走「未收录」，
        # 而不是编一段答案——编出来的内容没有任何文档能佐证。
        return None

    section_title, body = found
    answer = f"{scenario.lead}{body}"
    source = RagSource(
        source_file=scenario.source_file,
        document_title=_document_title(scenario.source_file),
        section_title=section_title,
        chunk_index=0,
        preview=build_preview(body),
        # demo 不做向量检索也不做精排，所以这些分数**都是 None，不是 0**。
        # 填 0 会让人看到「相似度 0 却排在第一位」这种自相矛盾的展示。
        distance=None,
        similarity=None,
        keyword_score=None,
        rrf_score=None,
        rerank_score=None,
    )
    return RagAnswer(
        status="ok",
        answer=f"{answer}\n\n（Demo 模式：以上内容摘自仓库内的知识文档，未调用检索与模型。）",
        sources=(source,),
        retrieval=RagRetrievalSummary(
            query_rewritten=False,
            query_count=1,
            candidates_considered=len(KNOWLEDGE_SCENARIOS),
            rerank_applied=False,
            final_count=1,
        ),
    )


def unsupported_answer() -> RagAnswer:
    """没收录的问题：给出结构化提示，且**明确说明没有调用模型**。"""
    lines = [
        "Demo 模式未收录这个问题。",
        "",
        "Demo 模式目前支持以下知识问答：",
    ]
    lines.extend(f"  · {s.question}" for s in KNOWLEDGE_SCENARIOS)
    lines.extend(
        [
            "",
            "以上问题之外，Demo 模式不会调用检索或模型，因此给不出有依据的回答。",
            "如需提问其它问题，请切换到真实模式。",
        ]
    )
    return RagAnswer(
        status="insufficient",
        answer="\n".join(lines),
        sources=(),
        retrieval=RagRetrievalSummary(
            query_rewritten=False,
            query_count=1,
            candidates_considered=len(KNOWLEDGE_SCENARIOS),
            rerank_applied=False,
            final_count=0,
        ),
    )


async def demo_answer_from_knowledge(question: str, *, top_k: int = 5) -> RagAnswer:
    """demo 版的知识问答入口，签名与 answer_from_knowledge 对齐。

    top_k 收下但不用：demo 的场景表是「一问一答」，没有可排序的候选集合。
    显式保留这个参数是为了让调用方（路由）不需要为两种模式写两套调用代码。
    """
    del top_k
    scenario = match_knowledge(question)
    if scenario is None:
        return unsupported_answer()
    return _answer_for(scenario) or unsupported_answer()


async def demo_knowledge_answerer(question: str) -> RagAnswer:
    """问数图里「只查知识库作答」那个节点用的替身。

    与 demo_answer_from_knowledge 的区别只是默认 top_k——图里那条路没有
    top_k 这个概念，所以单独包一层，而不是给调用方塞一个用不上的参数。
    """
    return await demo_answer_from_knowledge(question)


__all__ = [
    "demo_answer_from_knowledge",
    "demo_knowledge_answerer",
    "supported_questions",
    "unsupported_answer",
]
