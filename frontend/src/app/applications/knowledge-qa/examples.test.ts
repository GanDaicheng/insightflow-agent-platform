import { describe, expect, it } from "vitest";

import { KNOWLEDGE_QA_EXAMPLES } from "./examples";

describe("知识问答示例问题", () => {
  it("覆盖真实电商经营中的四类知识问题", () => {
    expect(KNOWLEDGE_QA_EXAMPLES.map((group) => group.label)).toEqual([
      "指标口径",
      "订单与数据关联",
      "促销与广告规则",
      "经营判断边界",
    ]);
    expect(KNOWLEDGE_QA_EXAMPLES).toHaveLength(4);
    expect(KNOWLEDGE_QA_EXAMPLES.every((group) => group.items.length === 3)).toBe(true);
  });

  it("问题使用知识库实际覆盖的电商业务表达", () => {
    const questions = KNOWLEDGE_QA_EXAMPLES.flatMap((group) => group.items);

    expect(questions).toEqual([
      "销售额应该用实付金额还是订单金额？",
      "毛利率怎么计算，是否包含广告费和运费？",
      "退款率应该按退款金额还是售后单数计算？",
      "按渠道分析订单和销售额需要关联哪些表？",
      "订单明细和商品主数据通过哪个字段关联？",
      "订单省份和发货仓省份有什么区别？",
      "促销期间销售额上涨，应该同时看哪些指标？",
      "广告归因销售额和公司实际销售额有什么区别？",
      "ROAS 高，能不能直接说明这个广告活动更赚钱？",
      "哪些 SKU 的毛利高但退款率也高，应该如何分析？",
      "哪些指标可以一起判断某省份物流体验较差？",
      "库存出现缺货时，能直接证明缺货造成了多少销售损失吗？",
    ]);
  });
});
