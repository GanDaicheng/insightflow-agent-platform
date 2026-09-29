/**
 * 平台总览页的内容配置。
 *
 * 为什么单独一个文件，而不是塞进 platform-config.ts？
 * platform-config 管的是**信息架构**（有哪些层级、哪些模块、路由是什么），
 * 路由解析、左侧导航和模块页都在读它。总览页的 Hero 文案、主链路节点、
 * 业务卡片的输入输出、面试亮点属于**这一页的叙事**，改它们不应该牵动导航。
 *
 * **不在这里重写的东西**：
 * - 技术分层、技术节点职责、三条工作流的步骤 → 复用 ARCHITECTURE_MODEL，
 *   架构页和导出图读的是同一份，写第二份迟早漂移。
 * - 路径 → 复用 PAGE_HREFS。
 * - 平台名与零售样例说明 → 复用 platform-config 的常量。
 *
 * **维护约定**：与 platform-config 一致——每句话都要能在代码或接口里找到对应物，
 * 这里不许出现完成率、成功率、性能数字这类没有出处的指标。
 */

// 相对导入而非 @/ 别名：vitest 没有配 paths 别名，用别名这个文件就没法被单测加载。
// architecture-data.ts 当初也是因此写成相对路径的。
import { ARCHITECTURE_MODEL } from "../architecture/architecture-data";

import {
  PAGE_HREFS,
  PLATFORM_NAME,
  RETAIL_DATA_NOTE,
} from "./platform-config";

/* ------------------------------- Hero ------------------------------- */

export const OVERVIEW_HERO = {
  title: PLATFORM_NAME,
  subtitle:
    "从业务文档与数据资产出发，通过 RAG、LangGraph 和 Deep Agents，交付可解释的知识问答、智能图表与经营分析。",
  /**
   * 三个能力标签，对应下面三张业务卡片。
   * 用各业务页自己的标题，不另起短名——首页上同一件事只该有一个叫法。
   */
  tags: [
    "RAG知识问答",
    "自然语言智能图表 Agent",
    "AI经营分析助手",
  ],
  note: RETAIL_DATA_NOTE,
};

/* ---------------------------- 平台主链路 ---------------------------- */

export type PipelineNode = {
  id: string;
  /** 节点名。 */
  name: string;
  /** 一句话说明这个环节解决什么问题。 */
  role: string;
  /** 关键技术与实现。只列这条链路上真正用到的。 */
  tech: string;
  /** 可跳转的真实页面。输入与输出两个端点没有页面可去，因此留空。 */
  href?: string;
  hrefLabel?: string;
};

/** 从输入到输出的完整闭环。顺序即数据流向。 */
export const OVERVIEW_PIPELINE: PipelineNode[] = [
  {
    id: "input",
    name: "业务文档 / 业务数据",
    role: "平台的两种输入：非结构化的知识文档，以及结构化的电商公司运营数据。",
    tech: "md / txt / docx / pdf · PostgreSQL 星型模型",
  },
  {
    id: "ingestion",
    name: "数据采集与知识入库",
    role: "把文档解析、切片、向量化后写进知识库，形成可检索的知识底座。",
    tech: "python-docx · pdfplumber · text-embedding-v4 · pgvector",
    href: PAGE_HREFS.dataSources,
    hrefLabel: "进入数据采集",
  },
  {
    id: "rag",
    name: "RAG 检索与业务口径",
    role: "改写问题后双路召回再融合精排，为回答和问数提供可核验的业务口径。",
    tech: "向量召回 + 关键词召回 · RRF · qwen3-rerank",
    href: PAGE_HREFS.knowledgeQa,
    hrefLabel: "进入知识问答",
  },
  {
    id: "agent",
    name: "Agent 编排与安全执行",
    role: "用状态图与主管 Agent 组织任务，生成的 SQL 经语法树校验后才落到只读查询。",
    tech: "LangGraph · Deep Agents · sqlglot AST · 只读事务",
    href: PAGE_HREFS.dataQuery,
    hrefLabel: "进入智能图表",
  },
  {
    id: "applications",
    name: "RAG知识问答 · 自然语言智能图表 Agent · AI经营分析助手",
    role: "三个业务入口，分别面向文档知识、结构化数据和复杂的经营目标。",
    tech: "带来源回答 · NL2SQL · 多轮 Agent 分析",
    href: PAGE_HREFS.businessAnalysis,
    hrefLabel: "进入经营分析",
  },
  {
    id: "output",
    name: "答案、数据结果、图表建议、分析报告",
    role: "平台的产出：可追溯的结论、可核对的来源，以及可汇报的分析报告。",
    tech: "来源引用 · 结果表格 · 图表建议 · SSE 流式报告",
  },
];

/* --------------------------- 四个业务入口 --------------------------- */

export type ApplicationEntry = {
  id: string;
  name: string;
  /** 它解决什么问题。用业务语言，不用技术语言。 */
  problem: string;
  input: string;
  /** 核心处理过程，按真实执行顺序排列。 */
  process: string[];
  output: string;
  href: string;
};

/**
 * 当前真实存在、点得进去的四个业务入口。
 *
 * 顺序按「先有知识底座、再有两条问答链路、最后是多步 Agent」排列。
 * 名称一律用**各业务页自己的标题**（数据采集 / RAG知识问答 /
 * 自然语言智能图表 Agent / AI经营分析助手），与左侧导航的短名
 * （数据采集 / 知识问答 / 智能图表 / AI 经营分析）不同——
 * 导航求短，卡片求准，同一功能点开后看到的名字应该和卡片上写的一致。
 */
export const OVERVIEW_APPLICATIONS: ApplicationEntry[] = [
  {
    id: "data-sources",
    name: "数据采集",
    problem: "知识散落在 Word 与 PDF 里，模型检索不到，口径也就没有统一出处。",
    input: "md / txt / docx / 带文字层的 pdf 知识文档",
    process: [
      "docx 按段落样式、PDF 按字号还原标题层级",
      "按二级标题切片并提取来源元数据",
      "计算向量后写入 PostgreSQL + pgvector",
      "内容未变时整体跳过，不重复消耗向量计算",
    ],
    output: "可检索的知识切片，以及本次入库的处理统计",
    href: PAGE_HREFS.dataSources,
  },
  {
    id: "knowledge-qa",
    name: "RAG知识问答",
    problem: "指标口径、业务规则、数据字典没有统一出处，只能靠人记或问人。",
    input: "自然语言问题：口径、规则、数据字典",
    process: [
      "查询改写",
      "向量召回 + 关键词召回",
      "RRF 融合排序",
      "qwen3-rerank 精排",
      "只依据检索到的资料生成回答",
    ],
    output: "回答，以及文档、小节、原文片段与相似度构成的来源",
    href: PAGE_HREFS.knowledgeQa,
  },
  {
    id: "data-query",
    name: "自然语言智能图表 Agent",
    problem: "业务人员不会写 SQL，取数要排队等数据团队，口径还容易对不齐。",
    input: "自然语言业务问题",
    process: [
      "意图识别",
      "数据资产发现：表、字段与可用指标",
      "RAG 补充指标口径与业务规则",
      "生成 SQL",
      "sqlglot AST 安全校验，失败时最多自动修复一次",
      "在只读事务中查询 PostgreSQL",
    ],
    output: "图表、结果表格和简短数据结论，需要解释口径时附知识库来源",
    href: PAGE_HREFS.dataQuery,
  },
  {
    id: "business-analysis",
    name: "AI经营分析助手",
    problem: "一次经营诊断要横跨多个指标和规则，人工很难把证据串成一条线。",
    input: "一句话的经营分析目标",
    process: [
      "Deep Agents 主管 Agent 拆解目标",
      "调用智能图表 LangGraph 工具取数",
      "调用 RAG 知识工具检索业务规则",
      "Agent Loop 多轮分析、按异常继续下钻",
      "PostgreSQL Checkpoint / Store 保存运行状态与偏好",
      "SSE 实时输出执行过程",
    ],
    output: "结构化经营分析报告，以及可回看的完整执行过程",
    href: PAGE_HREFS.businessAnalysis,
  },
];

/* ---------------------------- 面试亮点 ---------------------------- */

/**
 * 这个项目重点解决了什么问题。
 *
 * 三句话对应平台的三层价值：入口（人能问）、可信（答有据）、可控（执行有约束）。
 */
export const OVERVIEW_VALUE_PROPS = [
  {
    id: "ask",
    title: "让业务人员能问",
    detail: "自然语言直接提问，不需要会写 SQL，也不需要先找数据团队排期。",
  },
  {
    id: "grounded",
    title: "让模型回答有依据",
    detail: "回答前先从业务文档里检索证据，答案附带来源，资料不足时如实说明。",
  },
  {
    id: "controlled",
    title: "让 Agent 执行可控",
    detail: "SQL 经语法树校验、白名单与只读事务三重约束，失败最多自动修复一次。",
  },
] as const;

export type InterviewHighlight = {
  id: string;
  title: string;
  /** 一句普通中文解释。不罗列技术名，讲清楚这件事为什么值得做。 */
  detail: string;
};

export const INTERVIEW_HIGHLIGHTS: InterviewHighlight[] = [
  {
    id: "rag-nl2sql",
    title: "RAG 与 NL2SQL 融合",
    detail:
      "生成 SQL 之前，先用知识库补齐指标口径和业务规则，避免模型按自己的理解编一个口径出来。",
  },
  {
    id: "tool-calling",
    title: "Agent 工具调用与状态编排",
    detail:
      "把问数、知识检索、报告生成包装成工具，由 Agent 按任务需要自己决定调用顺序，而不是写死一条流水线。",
  },
  {
    id: "supervisor",
    title: "Deep Agents 主管 Agent",
    detail:
      "面对一个笼统的经营目标，主管 Agent 先把它拆成可执行的子任务，再逐个收集证据。",
  },
  {
    id: "safe-sql",
    title: "安全 SQL 执行闭环",
    detail:
      "模型生成的 SQL 先过语法树校验和表列白名单，再放进只读事务里限时执行；不通过时最多自动修复一次。",
  },
  {
    id: "sse",
    title: "SSE 实时展示 Agent 执行过程",
    detail:
      "分析不是黑盒等待：每一步工具调用和报告增量都实时推送到浏览器，过程本身可以复核。",
  },
  {
    id: "persistence",
    title: "Checkpoint 与 Store 状态持久化",
    detail:
      "运行状态与长期偏好落在 PostgreSQL 里，中断的分析可以恢复，报告可以回看。",
  },
  {
    id: "delivery",
    title: "前后端分离与 Docker 化部署",
    detail:
      "Next.js 前端、FastAPI 后端与 PostgreSQL 各自独立，用 Docker Compose 一条命令起停。",
  },
];

/* ---------------------------- 项目边界 ---------------------------- */

/**
 * 首页的边界清单。
 *
 * 前三条直接取架构页的数据源，不重写——架构页与导出图读的也是它。
 * 后三条是总览页需要单独说明的部分。
 *
 * 注意：**不要**往 ARCHITECTURE_MODEL.boundaries 里加条目来「统一」这份清单。
 * 导出 SVG 把边界按 `x = 54 + index * 610` 单行排布，画布只有 1920 宽，
 * 超过三条就会跑到画布外面去。
 */
export const PROJECT_BOUNDARY_INTRO =
  "当前为一家电商公司的运营样例，不代表平台只能服务电商场景。以下是这个作品集版本的真实边界，也是继续工程化时优先补齐的部分。";

export const PROJECT_BOUNDARY_ITEMS: string[] = [
  ...ARCHITECTURE_MODEL.boundaries,
  "企业真实生产数据尚未接入，当前所有结论都基于电商公司运营样例数据。",
  "RAG 检索质量的人工评测与反馈闭环尚未建立，检索效果目前只能靠人工试问判断。",
  "多轮会话上下文与跨会话记忆能力仍然有限，经营分析之外的场景每次提问相互独立。",
];

/* --------------------------- 技术栈分层 --------------------------- */

/**
 * 首页技术栈区域渲染的层级。
 *
 * 直接取架构模型里除「当前业务应用」之外的层——业务应用在上面已经有四张卡片，
 * 这里再列一遍是重复。过滤后正好是工程、资源、能力、编排、交付五层。
 */
export const OVERVIEW_TECH_LAYERS = [...ARCHITECTURE_MODEL.layers]
  .filter((layer) => layer.id !== "applications")
  .sort((left, right) => left.order - right.order);

export type OverviewTechNode = (typeof ARCHITECTURE_MODEL.nodes)[number];

/** 某层下的技术节点，按架构模型里的定义顺序。 */
export function getTechNodesByLayer(layerId: string): OverviewTechNode[] {
  return ARCHITECTURE_MODEL.nodes.filter((node) => node.layerId === layerId);
}
