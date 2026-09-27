/**
 * 运行模式（GET /api/v1/runtime）的客户端。
 *
 * 后端返回的是 APP_MODE：real 走真实模型与检索，demo 全部换成确定性替身。
 * 前端只知道一件事——**要不要显示 Demo 标识**，以及 demo 下可以问哪些问题。
 *
 * 拿不到模式时返回 null，调用方按「不显示横幅」处理：后端没起来的时候
 * 页面上本来就有别的提示，再叠一条「模式未知」只会让人以为配置出了问题。
 */

import { buildApiUrl, isAbortError, isRecord } from "./http";

const ENDPOINT_PATH = "/api/v1/runtime";

export type RuntimeMode = {
  appMode: "real" | "demo";
  demo: boolean;
  /** 仅 demo 模式有内容：能力名 → 该能力支持的问题清单。 */
  supportedQuestions: Record<string, string[]>;
};

function readSupportedQuestions(value: unknown): Record<string, string[]> {
  if (!isRecord(value)) return {};

  const result: Record<string, string[]> = {};
  for (const [group, questions] of Object.entries(value)) {
    if (!Array.isArray(questions)) continue;
    const usable = questions.filter(
      (question): question is string => typeof question === "string" && question.length > 0,
    );
    // 丢掉空清单：一个「有标题但没有内容」的分组只会让提示区出现空段落
    if (usable.length > 0) result[group] = usable;
  }
  return result;
}

export async function fetchRuntimeMode(signal?: AbortSignal): Promise<RuntimeMode | null> {
  let response: Response;

  try {
    response = await fetch(buildApiUrl(ENDPOINT_PATH), {
      method: "GET",
      // 模式可能在两次访问之间被改掉（重启后端换了 APP_MODE），不能缓存
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    return null;
  }

  if (!response.ok) return null;

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return null;
  }

  if (!isRecord(payload)) return null;

  // 只认后端明确说的 demo=true。字段缺失、类型不对、接口被别的进程占用，
  // 一律当成 real —— 宁可不显示横幅，也不能把真实模式误标成 Demo。
  const demo = payload.demo === true;

  return {
    appMode: demo ? "demo" : "real",
    demo,
    supportedQuestions: demo ? readSupportedQuestions(payload.supported_questions) : {},
  };
}
