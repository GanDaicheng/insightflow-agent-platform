"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { isAbortError } from "@/lib/api/http";
import {
  createAnonymousUserId,
  deleteAnalysisThread,
  loadAnalysisThread,
  loadAnalysisThreads,
  runBusinessAnalysis,
} from "@/lib/api/business-analysis";

import { AnalysisComposer } from "@/components/business-analysis/AnalysisComposer";
import { AnalysisMessageList } from "@/components/business-analysis/AnalysisMessageList";
import { AnalysisThreadList } from "@/components/business-analysis/AnalysisThreadList";
import styles from "./business-analysis.module.css";
import {
  CANCELLED_STATUS_TEXT,
  GENERIC_ERROR_TEXT,
  INCOMPLETE_STREAM_TEXT,
  createMessagesFromRuns,
  reduceAssistantEvent,
  type ChatMessage,
} from "./business-analysis-view-model";

type Phase = "idle" | "running" | "done" | "cancelled" | "failed";
type Thread = { id: string; title: string; updatedAt: string };

/** 示例只填入输入框，不直接发送——点一下就是一次真实的模型调用，太重了。 */
const EXAMPLES = [
  {
    label: "分析某省第三季度销售下降原因…",
    question:
      "分析某省第三季度销售下降原因，找出影响最大的品类，并结合促销规则给出建议。",
  },
  {
    label: "分析退款率较高的品类…",
    question: "分析退款率较高的品类，结合售后原因和物流时效解释差异并给出建议。",
  },
  {
    label: "分析全年销售额季节性变化…",
    question: "分析今年销售额的季节性变化，结合促销日历判断可能原因。",
  },
];

function now(): string {
  return new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function newThread(): Thread {
  return { id: crypto.randomUUID(), title: "新的经营分析", updatedAt: now() };
}

function Welcome({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className={styles.welcome}>
      <h2 className={styles.welcomeTitle}>AI经营分析助手</h2>
      <p className={styles.welcomeText}>
        可以帮你分析经营数据、核对指标口径，并结合业务知识给出建议。
      </p>
      <ul className={styles.welcomeExamples}>
        {EXAMPLES.map((example) => (
          <li key={example.label}>
            <button type="button" onClick={() => onPick(example.question)}>
              {example.label}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function BusinessAnalysisWorkspace() {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeThread, setActiveThread] = useState<Thread>(() => newThread());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [threadError, setThreadError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  /** 打开历史会话是异步的，这个序号用来丢弃「切走之后才回来的」旧响应。 */
  const loadSeqRef = useRef(0);
  const userId = useMemo(() => createAnonymousUserId(), []);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    void loadAnalysisThreads(userId)
      .then((items) => {
        setThreads(items.map((item) => ({
          id: item.thread_id,
          title: item.title,
          updatedAt: item.updated_at
            ? new Date(item.updated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
            : "历史会话",
        })));
      })
      .catch(() => undefined);
  }, [userId]);

  // 每来一个事件就贴底。用即时滚动而不是平滑滚动：流式阶段一秒可能滚十几次，
  // 平滑滚动会让整段回答一直在动，读起来是抖的。
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "auto" });
  }, [messages]);

  const patchMessage = useCallback(
    (id: string, update: (message: ChatMessage) => ChatMessage) => {
      setMessages((current) =>
        current.map((message) => (message.id === id ? update(message) : message)),
      );
    },
    [],
  );

  const submit = useCallback(async () => {
    const text = question.trim();
    if (!text || phase === "running") return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const assistantId = crypto.randomUUID();
    const thread = { ...activeThread, title: text.slice(0, 28), updatedAt: now() };

    setPhase("running");
    setQuestion("");
    setActiveThread(thread);
    setThreads((current) => [
      thread,
      ...current.filter((item) => item.id !== thread.id),
    ]);
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", content: text, status: "complete" },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        status: "streaming",
        statusText: "正在理解你的问题…",
      },
    ]);

    // 连接正常关闭不等于分析正常结束：中途断流时后端不会再发 run_completed，
    // 不盯着这个标志就会把一条空回答当成成功留在页面上。
    let completed = false;
    try {
      await runBusinessAnalysis(
        { threadId: thread.id, message: text, userId },
        controller.signal,
        (event) => {
          if (event.type === "run_completed") completed = true;
          patchMessage(assistantId, (message) => reduceAssistantEvent(message, event));
        },
      );
      if (completed) {
        setPhase("done");
      } else {
        patchMessage(assistantId, (message) => ({
          ...message,
          status: "error",
          statusText: INCOMPLETE_STREAM_TEXT,
        }));
        setPhase("failed");
      }
    } catch (requestError) {
      if (completed) {
        // run_completed 已经到了，报告是完整的；之后的连接抖动不改结论。
        setPhase("done");
      } else if (isAbortError(requestError)) {
        patchMessage(assistantId, (message) => ({
          ...message,
          status: "cancelled",
          statusText: CANCELLED_STATUS_TEXT,
        }));
        setPhase("cancelled");
      } else {
        // 错误事件已经带着具体原因写进消息了，这里只补 HTTP 层就没走到事件流的情况。
        patchMessage(assistantId, (message) =>
          message.status === "error"
            ? message
            : { ...message, status: "error", statusText: GENERIC_ERROR_TEXT },
        );
        setPhase("failed");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [activeThread, patchMessage, phase, question, userId]);

  const selectThread = useCallback(
    async (thread: Thread) => {
      if (phase === "running") return;
      abortRef.current?.abort();
      const seq = (loadSeqRef.current += 1);
      setActiveThread(thread);
      setMessages([]);
      setPhase("idle");
      try {
        const runs = await loadAnalysisThread(thread.id);
        if (loadSeqRef.current !== seq) return;
        setMessages(createMessagesFromRuns(runs));
      } catch {
        if (loadSeqRef.current !== seq) return;
        setMessages([
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: "",
            status: "error",
            statusText: "历史会话读取失败，请稍后重试。",
          },
        ]);
      }
    },
    [phase],
  );

  const cancel = useCallback(() => abortRef.current?.abort(), []);

  const startNew = useCallback(() => {
    abortRef.current?.abort();
    loadSeqRef.current += 1;
    setActiveThread(newThread());
    setMessages([]);
    setQuestion("");
    setPhase("idle");
    setThreadError(null);
  }, []);

  const deleteThread = useCallback(async (thread: Thread) => {
    if (phase === "running") return;
    if (!window.confirm(`确定删除“${thread.title}”吗？删除后无法恢复。`)) return;

    setThreadError(null);
    try {
      await deleteAnalysisThread(thread.id, userId);
      setThreads((current) => current.filter((item) => item.id !== thread.id));
      if (activeThread.id === thread.id) {
        loadSeqRef.current += 1;
        abortRef.current?.abort();
        setActiveThread(newThread());
        setMessages([]);
        setQuestion("");
        setPhase("idle");
      }
    } catch {
      setThreadError("删除失败，请稍后重试。");
    }
  }, [activeThread.id, phase, userId]);

  const pickExample = useCallback((value: string) => {
    setQuestion(value);
    inputRef.current?.focus();
  }, []);

  return (
    <section className={styles.workspace} aria-label="AI 经营分析">
      <AnalysisThreadList
        threads={threads}
        activeId={activeThread.id}
        busy={phase === "running"}
        onNew={startNew}
        onSelect={(thread) => void selectThread(thread)}
        onDelete={(thread) => void deleteThread(thread)}
        footer={threadError ? <p className={styles.threadError} role="alert">{threadError}</p> : null}
      />
      <div className={styles.chatColumn}>
        <header className={styles.chatHeader}>
          <h1 className={styles.chatTitle}>AI经营分析助手</h1>
          <p className={styles.chatThreadTitle}>{activeThread.title}</p>
        </header>
        <div className={styles.chatScroll}>
          {messages.length === 0 && phase !== "running" ? (
            <Welcome onPick={pickExample} />
          ) : null}
          <AnalysisMessageList messages={messages} bottomRef={bottomRef} />
        </div>
        <AnalysisComposer
          value={question}
          phase={phase}
          inputRef={inputRef}
          onChange={setQuestion}
          onSend={() => void submit()}
          onStop={cancel}
        />
      </div>
    </section>
  );
}
