import { describe, expect, it } from "vitest";

import { EXAMPLES } from "./examples";

describe("智能问数示例问题", () => {
  it("只展示面向真实电商运营决策且有明确图表输出的问题", () => {
    const items = EXAMPLES.flatMap((group) => group.items);

    expect(EXAMPLES.map((group) => group.label)).toEqual([
      "经营趋势",
      "商品与品类",
      "广告投放",
      "履约与售后",
    ]);
    expect(items).toHaveLength(8);
    expect(items.every((item) => item.question.includes("2025") || item.question.includes("当前"))).toBe(true);
    expect(items.every((item) => ["折线图", "柱状图"].includes(item.chartLabel))).toBe(true);
    expect(items.every((item) => item.context.length > 0)).toBe(true);
    expect(items.map((item) => item.id)).toEqual([
      "monthly-sales",
      "monthly-orders",
      "top-skus",
      "category-sales-units",
      "campaign-sales",
      "monthly-refunds",
      "delivery-time",
      "after-sales-reasons",
    ]);
  });
});
