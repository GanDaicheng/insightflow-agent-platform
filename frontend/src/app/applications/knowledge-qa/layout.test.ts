import { describe, expect, it } from "vitest";

import { getKnowledgeQaGridTemplate } from "./knowledge-qa-layout";

describe("知识问答输入区布局", () => {
  it("给检索设置保留紧凑的右侧栏，并把主输入区放在左侧", () => {
    expect(getKnowledgeQaGridTemplate()).toBe("minmax(0, 1fr) 240px");
  });
});
