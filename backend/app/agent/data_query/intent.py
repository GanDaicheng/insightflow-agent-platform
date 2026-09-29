"""意图识别模块：把自然语言问题交给模型，识别成受约束的意图枚举。

职责只有一件：输入问题 → 调用既有 LLM → 返回 IntentClassification。
不生成 SQL、不查资产、不访问数据库。

本模块是 data_query 包里唯一允许接入模型的地方，而且必须走
app/core/llm.py 这个项目统一的入口——模型名、Base URL、API Key 全部由
core 层的配置决定，这里不重复任何一份，也不直接读环境变量。

Prompt、Schema、意图枚举为什么都放在本文件？
它们是同一份契约的三个视角：Prompt 告诉模型有哪些选项，Literal 上了类型锁，
Pydantic 模型在返回值上做校验。三者必须同步修改，放在一起改一处就够，
拆到 prompts.py 反而增加「改了 A 忘了 B」的风险。
等以后意图之外的 Prompt 多起来（SQL 生成、结论解释各有各的 Prompt），
再拆独立的 prompts 文件也不迟。
"""

from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from app.agent.data_query.state import Intent

# 结构化输出用哪种方式，见下方 STRUCTURED_OUTPUT_METHOD 的说明
STRUCTURED_OUTPUT_METHOD: Literal["function_calling", "json_schema", "json_mode"] = (
    "function_calling"
)

INTENT_PROMPT = """你是一个意图分类器，只做一件事：判断用户问题的分析意图。

可选意图（只能选其中一个，不得自造）：
- trend：按时间观察指标变化，例如趋势、走势、近几个月的涨跌
- ranking：按指标大小排序取前几名，例如排行、TOP N、最高、最低
- breakdown：按区域、商品、会员等级等维度拆分对比
- repurchase：复购、重复购买、会员回购相关分析
- unknown：与数据分析无关的问题（闲聊、写诗、问天气等），或无法判断

reason 用一句简短中文说明判断依据。

严格约束：
1. 只输出 intent 和 reason，不要输出任何其他内容。
2. 不要生成 SQL，不要虚构表、字段、指标，不要编造查询结果。
3. 不要调用任何工具。
4. 不要回答用户的业务问题——你的职责只是分类，不是答题。
5. 拿不准就选 unknown，不要勉强归类。"""


class IntentClassification(BaseModel):
    """意图识别的返回结构。

    这两个字段同时也是给模型看的「输出契约」：langchain 会把本模型转成
    JSON Schema 交给模型，模型只能在这个形状里作答。
    """

    intent: Intent = Field(description="问题意图，只能取约定好的五个值之一")
    reason: str = Field(description="用一句简短中文说明为什么这么分类")


def classify_intent(
    question: str, *, llm: BaseChatModel | None = None
) -> IntentClassification:
    """把问题交给模型分类，返回经过 Pydantic 校验的结果。

    llm 参数用于测试时注入替身；不传时使用项目统一的 LLM 工厂，
    模型名与地址完全由 core 层配置决定。

    为什么用 method="function_calling" 而不是默认的 "json_schema"？
    ChatOpenAI.with_structured_output 的默认值是 "json_schema"，它走的是
    OpenAI 专有的 Structured Outputs API，会发 response_format=
    {"type": "json_schema"}。本项目的 .env.example 列出的是 DeepSeek / 通义千问 /
    月之暗面这类 OpenAI *兼容* 服务，它们只兼容 /chat/completions 的基本形状，
    并不实现这套专有协议，发过去会被拒。
    "function_calling" 走的是工具调用（tool calling），这是兼容服务普遍支持的
    能力，而且返回结果同样会落到 IntentClassification 上做 Pydantic 校验——
    既不用手写正则解析，也没有降级成「自由文本 + 关键词猜意图」。
    """
    model = llm if llm is not None else _production_llm()
    structured = model.with_structured_output(
        IntentClassification, method=STRUCTURED_OUTPUT_METHOD
    )
    return structured.invoke(
        [("system", INTENT_PROMPT), ("human", question)]
    )


def _production_llm() -> BaseChatModel:
    """延迟取用统一 LLM 工厂。

    故意写在函数里而不是模块顶层调用：get_llm() 在缺少 API Key 时会抛
    ConfigurationError，我们希望这个错误发生在「真的要调用模型」的时刻，
    而不是 import 本模块的时刻——否则测试和 CI 光是导入就会炸。
    """
    from app.core.llm import get_llm

    return get_llm()
