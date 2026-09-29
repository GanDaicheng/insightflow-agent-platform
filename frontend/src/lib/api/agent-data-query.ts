/**
 * 智能问数接口的客户端。
 *
 * 职责只有四件事：定义类型、发请求、把 HTTP 失败映射成页面能用的错误、返回响应。
 * 它不认识 React，也不做任何展示格式化——那样才能被单独测试和复用。
 *
 * 安全约定：
 * - 只发 question 一个字段，不附带 SQL / intent / retry_count 等任何内部状态；
 * - 不把后端返回的 detail 原样交给页面（那是内部信息）；
 * - 不打印问题全文，也不打印整份响应（问题属于用户数据）。
 */

/**
 * 拼接后端地址、取消判断、对象判断都来自 ./http —— 三个客户端本来各抄了一份，
 * 一模一样地抄，改一处就得记着改三处。
 */
import { buildApiUrl, isAbortError, isRecord } from "./http";

export type QueryResultSource = "postgres" | "mock";

export type QueryResult = {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number;
  source: QueryResultSource;
};

export type ChartType = "line" | "bar" | "table" | "none";
export type ValueFormat = "currency" | "number" | "percent";

/**
 * 契约里这两个枚举的合法取值。
 *
 * 写在这里而不是散在校验函数里，是为了让「类型定义」和「运行时校验」看同一份清单——
 * 两处各写一份的话，将来加一种图表类型极容易出现「类型上允许、校验里被拒」。
 */
const CHART_TYPES: readonly ChartType[] = ["line", "bar", "table", "none"];
const VALUE_FORMATS: readonly ValueFormat[] = ["currency", "number", "percent"];

export type ChartSuggestion = {
  chart_type: ChartType;
  title: string;
  x_field: string | null;
  y_field: string | null;
  /** 同一纵轴上可安全比较的指标；y_field 是主指标。 */
  y_fields: string[];
  series_field: string | null;
  value_format: ValueFormat | null;
  reason: string;
};

/**
 * 回答参考的一条知识库资料。
 *
 * 注意它**只是解释的来源，不是数字的来源**——数字永远来自 query_result。
 * 页面上的说明文字必须把这条边界讲清楚，否则用户会以为这些文档是数据出处。
 *
 * 标成可选是为了兼容还没上线这个字段的后端：缺席时按空数组处理，
 * 页面只是不显示这一块，其它字段照常工作。
 */
export type KnowledgeSource = {
  source_file: string;
  document_title: string;
  section_title: string;
  similarity: number;
  preview: string;
};

export type AgentDataQueryResponse = {
  status: "ok" | "error";
  answer: string;
  query_result: QueryResult | null;
  chart_suggestion: ChartSuggestion | null;
  events: string[];
  knowledge_sources?: KnowledgeSource[];
};

/** 后端监听的端口与地址拼接见 ./http。 */

/**
 * question 的长度上限，与后端 AgentDataQueryRequest 的 max_length 保持一致。
 * 前端先拦一道只是为了少发一次注定 422 的请求，真正的把关仍在后端。
 */
export const QUESTION_MAX_LENGTH = 500;

/** 页面可以直接展示的错误。区分类型是为了让 UI 选择不同的视觉语义。 */
export type AgentDataQueryErrorKind = "network" | "server" | "invalid";

const ERROR_MESSAGE: Record<AgentDataQueryErrorKind, string> = {
  network: "暂时无法连接智能问数服务，请确认后端服务已启动。",
  server: "智能问数服务返回异常，请稍后重试。",
  invalid: "智能问数服务返回的内容无法识别，请稍后重试。",
};

export class AgentDataQueryError extends Error {
  readonly kind: AgentDataQueryErrorKind;

  constructor(kind: AgentDataQueryErrorKind) {
    // 只用固定的中文文案，绝不把后端的 detail 或异常原文拼进来
    super(ERROR_MESSAGE[kind]);
    this.name = "AgentDataQueryError";
    this.kind = kind;
  }
}

/**
 * 拼接后端地址、取消判断、对象判断都来自 ./http（见文件顶部的 import）。
 */

function isQueryResult(value: unknown): value is QueryResult {
  if (!isRecord(value)) return false;
  if (!Array.isArray(value.columns) || !value.columns.every((c) => typeof c === "string")) {
    return false;
  }
  if (!Array.isArray(value.rows) || !value.rows.every(isRecord)) return false;
  if (typeof value.row_count !== "number") return false;
  if (value.source !== "postgres" && value.source !== "mock") return false;
  // 后端保证这两个数一致；对不上说明契约被破坏了，宁可报错也不要展示
  // 一个「N 行」却渲染出别的行数的表格
  return value.row_count === value.rows.length;
}

function isChartType(value: unknown): value is ChartType {
  return typeof value === "string" && (CHART_TYPES as readonly string[]).includes(value);
}

function isValueFormat(value: unknown): value is ValueFormat {
  return typeof value === "string" && (VALUE_FORMATS as readonly string[]).includes(value);
}

/** 契约允许这三个字段为 null（例如表格建议没有 y 轴），但一旦有值就必须是字符串。 */
function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

/**
 * 图表建议的校验。
 *
 * 七个字段**每一个都判**，包括 chart_type 与 value_format 的枚举取值：
 * 页面是拿着这些值直接决定「画折线还是画柱状」并按 value_format 格式化的，
 * 放进来一个没见过的 chart_type，轻则画错图，重则拿着 undefined 去取列。
 * 只判断「是个字符串」是不够的。
 */
function isChartSuggestion(value: unknown): value is ChartSuggestion {
  if (!isRecord(value)) return false;
  return (
    isChartType(value.chart_type) &&
    typeof value.title === "string" &&
    isNullableString(value.x_field) &&
    isNullableString(value.y_field) &&
    Array.isArray(value.y_fields) &&
    value.y_fields.every((field) => typeof field === "string") &&
    isNullableString(value.series_field) &&
    (value.value_format === null || isValueFormat(value.value_format)) &&
    typeof value.reason === "string"
  );
}

function isKnowledgeSource(value: unknown): value is KnowledgeSource {
  if (!isRecord(value)) return false;
  return (
    typeof value.source_file === "string" &&
    typeof value.document_title === "string" &&
    typeof value.section_title === "string" &&
    Number.isFinite(value.similarity) &&
    typeof value.preview === "string"
  );
}

/**
 * 知识来源是**可选**的：后端老版本没有这个字段。
 *
 * 缺席时放行（页面按空数组处理），但一旦出现就必须是合法数组——
 * 类型不对说明契约被改坏了，那种情况要报出来，不能悄悄当没有。
 */
function isKnowledgeSources(value: unknown): value is KnowledgeSource[] {
  return Array.isArray(value) && value.every(isKnowledgeSource);
}

function isAgentDataQueryResponse(value: unknown): value is AgentDataQueryResponse {
  if (!isRecord(value)) return false;
  if (value.status !== "ok" && value.status !== "error") return false;
  if (typeof value.answer !== "string") return false;
  if (!Array.isArray(value.events) || !value.events.every((e) => typeof e === "string")) {
    return false;
  }
  if (value.query_result !== null && !isQueryResult(value.query_result)) return false;
  if (value.chart_suggestion !== null && !isChartSuggestion(value.chart_suggestion)) {
    return false;
  }
  if (value.knowledge_sources !== undefined && !isKnowledgeSources(value.knowledge_sources)) {
    return false;
  }
  return true;
}

/**
 * 提交一个问题，返回 Agent 的完整回答。
 *
 * signal 用于「取消本次请求」：取消时 fetch 会抛 AbortError，
 * 这里**原样往上抛**——取消是用户的正常操作，不该被包装成服务故障。
 */
export async function queryAgent(
  question: string,
  signal?: AbortSignal,
): Promise<AgentDataQueryResponse> {
  let response: Response;

  try {
    response = await fetch(buildApiUrl("/api/v1/agent/data-query"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // 只发 question：请求体里出现别的字段就说明有地方越界了
      body: JSON.stringify({ question }),
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new AgentDataQueryError("network");
  }

  if (!response.ok) {
    // 刻意不读 response.json() 里的 detail：那是后端内部信息，
    // 页面只需要知道「服务这次没给出可用结果」。
    throw new AgentDataQueryError("server");
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new AgentDataQueryError("invalid");
  }

  if (!isAgentDataQueryResponse(payload)) {
    throw new AgentDataQueryError("invalid");
  }

  return payload;
}
