import { buildApiUrl } from "./http";

export type BusinessAnalysisEvent =
  | { type: "run_started"; run_id: string }
  | { type: "status"; label: string }
  | { type: "tool_started"; tool: string }
  | { type: "tool_completed"; tool: string; summary?: string }
  | { type: "report_delta"; content: string }
  | { type: "run_completed"; run_id: string; report_id?: string }
  | { type: "error"; error_code: string };

export type BusinessAnalysisInput = {
  threadId: string;
  message: string;
  userId?: string;
};

export type AnalysisThreadRun = {
  id: string;
  thread_id: string;
  title: string;
  status: string;
  report_id?: string | null;
  report?: { summary?: string } | null;
  created_at?: string;
  updated_at?: string;
};

export type AnalysisThreadSummary = {
  thread_id: string;
  title: string;
  status: string;
  report_id?: string | null;
  updated_at?: string;
};

export type UserPreferenceKey =
  | "currency_unit"
  | "preferred_region"
  | "preferred_chart"
  | "report_style";

const EVENT_TYPES = new Set<BusinessAnalysisEvent["type"]>([
  "run_started",
  "status",
  "tool_started",
  "tool_completed",
  "report_delta",
  "run_completed",
  "error",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseFrame(frame: string): BusinessAnalysisEvent | null {
  let eventType = "";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) eventType = line.slice(6).trim();
    if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!EVENT_TYPES.has(eventType as BusinessAnalysisEvent["type"])) return null;
  try {
    const parsed: unknown = JSON.parse(data);
    if (!isRecord(parsed)) return null;
    return {
      ...parsed,
      type: eventType,
    } as BusinessAnalysisEvent;
  } catch {
    return null;
  }
}

export function parseSseChunk(chunk: string): BusinessAnalysisEvent[] {
  return chunk
    .split("\n\n")
    .filter((frame) => frame.trim().length > 0)
    .map(parseFrame)
    .filter((event): event is BusinessAnalysisEvent => event !== null);
}

export function createAnonymousUserId(): string {
  const key = "ai_data_platform_user_id";
  try {
    const existing = window.localStorage.getItem(key);
    if (existing) return existing;
    const created = crypto.randomUUID();
    window.localStorage.setItem(key, created);
    return created;
  } catch {
    return crypto.randomUUID();
  }
}

function deliverEvent(
  event: BusinessAnalysisEvent,
  onEvent: (event: BusinessAnalysisEvent) => void,
): void {
  onEvent(event);
  if (event.type === "error") {
    const message = event.error_code === "AGENT_CONFIGURATION_ERROR"
      ? "模型服务尚未配置，请在 .env 中填写 OPENAI_API_KEY 后重启服务。"
      : "经营分析没有完成，请查看执行过程或稍后重试。";
    throw new Error(message);
  }
}

export async function runBusinessAnalysis(
  input: BusinessAnalysisInput,
  signal: AbortSignal,
  onEvent: (event: BusinessAnalysisEvent) => void,
): Promise<void> {
  const response = await fetch(buildApiUrl("/api/v1/agent/business-analysis/runs"), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      thread_id: input.threadId,
      message: input.message,
      user_id: input.userId,
    }),
    signal,
  });
  if (!response.ok) {
    throw new Error(`经营分析请求失败（${response.status}）。`);
  }
  if (!response.body) throw new Error("经营分析服务没有返回事件流。");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const boundary = buffer.lastIndexOf("\n\n");
    if (boundary >= 0) {
      for (const event of parseSseChunk(buffer.slice(0, boundary + 2))) {
        deliverEvent(event, onEvent);
      }
      buffer = buffer.slice(boundary + 2);
    }
    if (done) break;
  }
  if (buffer.trim()) {
    for (const event of parseSseChunk(`${buffer}\n\n`)) deliverEvent(event, onEvent);
  }
}

export async function loadAnalysisThread(threadId: string): Promise<AnalysisThreadRun[]> {
  const response = await fetch(
    buildApiUrl(`/api/v1/agent/business-analysis/threads/${encodeURIComponent(threadId)}`),
  );
  if (!response.ok) throw new Error(`读取分析会话失败（${response.status}）。`);
  return (await response.json()) as AnalysisThreadRun[];
}

export async function loadAnalysisThreads(userId: string): Promise<AnalysisThreadSummary[]> {
  const response = await fetch(
    buildApiUrl(`/api/v1/agent/business-analysis/threads?user_id=${encodeURIComponent(userId)}`),
  );
  if (!response.ok) throw new Error(`读取分析会话列表失败（${response.status}）。`);
  return (await response.json()) as AnalysisThreadSummary[];
}

export async function deleteAnalysisThread(threadId: string, userId: string): Promise<void> {
  const response = await fetch(
    buildApiUrl(
      `/api/v1/agent/business-analysis/threads/${encodeURIComponent(threadId)}?user_id=${encodeURIComponent(userId)}`,
    ),
    { method: "DELETE" },
  );
  if (!response.ok) throw new Error(`删除分析会话失败（${response.status}）。`);
}

export async function loadUserPreferences(userId: string): Promise<Record<string, unknown>> {
  const response = await fetch(
    buildApiUrl(`/api/v1/agent/business-analysis/preferences/${encodeURIComponent(userId)}`),
  );
  if (!response.ok) throw new Error(`读取用户偏好失败（${response.status}）。`);
  return (await response.json()) as Record<string, unknown>;
}

export async function saveUserPreference(
  userId: string,
  key: UserPreferenceKey,
  value: string,
): Promise<void> {
  const response = await fetch(
    buildApiUrl(`/api/v1/agent/business-analysis/preferences/${encodeURIComponent(userId)}`),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    },
  );
  if (!response.ok) throw new Error(`保存用户偏好失败（${response.status}）。`);
}
