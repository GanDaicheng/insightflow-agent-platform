import { describe, expect, it } from "vitest";

import { getChartFieldLabel } from "./chart-labels";

describe("图表字段名称", () => {
  it("把电商运营字段翻译成业务人员看得懂的名称", () => {
    expect(getChartFieldLabel("product_id")).toBe("SKU");
    expect(getChartFieldLabel("category_name")).toBe("品类");
    expect(getChartFieldLabel("campaign_name")).toBe("广告活动");
    expect(getChartFieldLabel("avg_delivery_days")).toBe("平均配送天数");
  });
});
