import { afterEach, describe, expect, it, vi } from "vitest";

import { deleteAnalysisThread, parseSseChunk, runBusinessAnalysis } from "./business-analysis";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseSseChunk", () => {
  it("parses multiple SSE events and preserves report deltas", () => {
    const events = parseSseChunk(
      'event: status\ndata: {"label":"正在查询"}\n\n' +
        'event: report_delta\ndata: {"content":"## 结论"}\n\n',
    );

    expect(events).toEqual([
      { type: "status", label: "正在查询" },
      { type: "report_delta", content: "## 结论" },
    ]);
  });

  it("ignores malformed frames instead of throwing", () => {
    expect(parseSseChunk("event: status\ndata: not-json\n\n")).toEqual([]);
  });

  it("rejects when the server streams an analysis error", async () => {
    vi.stubGlobal("window", {
      location: { protocol: "http:", hostname: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response('event: error\ndata: {"type":"error","error_code":"AGENT_CONFIGURATION_ERROR"}\n\n'),
      ),
    );
    const onEvent = vi.fn();

    await expect(
      runBusinessAnalysis(
        { threadId: "thread-1", message: "分析销售额" },
        new AbortController().signal,
        onEvent,
      ),
    ).rejects.toThrow("模型服务尚未配置");
    expect(onEvent).toHaveBeenCalledWith({
      type: "error",
      error_code: "AGENT_CONFIGURATION_ERROR",
    });
  });
});

describe("deleteAnalysisThread", () => {
  it("deletes one thread for the current user", async () => {
    vi.stubGlobal("window", {
      location: { protocol: "http:", hostname: "localhost" },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await deleteAnalysisThread("thread/1", "user-1");

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/agent/business-analysis/threads/thread%2F1"),
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(fetchMock.mock.calls[0][0]).toContain("user_id=user-1");
  });
});
