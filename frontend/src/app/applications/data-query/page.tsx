import type { Metadata } from "next";

import { PageHeader } from "@/components/ui/PageHeader";
import { ApplicationFlowDiagram } from "@/components/ui/ApplicationFlowDiagram";
import { DataQueryWorkspace } from "@/components/data-query/DataQueryWorkspace";
import { getModule, getSection, RETAIL_DATA_NOTE } from "@/features/platform/platform-config";

const SECTION_ID = "applications";
const MODULE_SLUG = "data-query";

export const metadata: Metadata = {
  title: "自然语言智能图表 Agent",
  description: "用自然语言查询电商公司运营数据，自动生成图表和可核对的明细。",
};

/**
 * 示例问题。
 *
 * 定义在服务端组件里、以 props 传给客户端工作台——数组是可序列化的，
 * 不会把整份配置或函数带进客户端包。
 *
 * 两条挑选原则：
 * 1. **写明年份与范围**。演示数据覆盖 2024—2026 年，写明时间范围可以让
 *    图表更容易复核，也避免把不同运营周期混在一起。
 * 2. 只放**当前安全策略真的能算出来**的问题。受控查询只允许单条 SELECT，
 *    函数限定在 COUNT / SUM / AVG / MIN / MAX，并且明确拒绝 CASE 与子查询
 *    （已实测：`HAVING` 放行，`CASE` 报「使用了不允许的 SQL 函数」，
 *    子查询报「禁止使用子查询」）。
 *    所以像「复购率」这种要算「下单次数大于 1 的客户数 ÷ 全部客户数」的比值，
 *    在这套限制下表达不出来——它要么需要条件计数（CASE），要么需要子查询。
 *    指标目录里登记了它，但问数答不了，不放进示例免得用户点了就撞墙。
 *
 * 点击示例只填入输入框，不自动提交：每次提问都会真实调用模型服务，
 * 不能因为一次误点就花掉一次调用。
 */
const EXAMPLES = [
  {
    label: "销售与订单",
    items: ["2025 年每月销售额和毛利趋势怎么样？", "2025 年每月订单数和客单价如何变化？"],
  },
  {
    label: "商品与省份",
    items: ["2025 年销售额最高的 10 个 SKU 是哪些？", "各省销售额排名和订单数有什么差异？"],
  },
  {
    label: "渠道与广告",
    items: ["2025 年不同销售渠道的销售额如何对比？", "哪些广告活动的 ROAS 最高？"],
  },
  {
    label: "库存与售后",
    items: ["哪些 SKU 的缺货率最高？", "各品类退款率和退款金额如何对比？"],
  },
] as const;

/**
 * 智能问数页面。
 *
 * 路由文件刻意保持很薄：只负责页头、边界说明和示例清单这些静态内容，
 * 交互与取数全部交给 DataQueryWorkspace（客户端组件）。
 * 这样绝大部分内容仍然是服务端渲染的。
 */
export default function Page() {
  const section = getSection(SECTION_ID);
  const moduleConfig = getModule(SECTION_ID, MODULE_SLUG);

  return (
    <article>
      <PageHeader
        sectionName={section?.name ?? "智能应用"}
        title="自然语言智能图表 Agent"
        align="center"
        subtitle="用自然语言查询电商公司运营数据，自动生成图表、明细和数据口径。"
        boundary={
          <>
            {/* 边界说明放在最顶上：用户提交的内容会被送到模型服务，
                这件事必须在动手输入之前就讲清楚 */}
            <p>
              当前为一家电商公司的内部运营样例数据，<strong>覆盖 2024—2026 年</strong>，
              包含订单、SKU、品类、省份、渠道、广告、库存、物流和售后数据。
              提交问题会调用模型服务，不适合输入敏感信息。
            </p>
            <p>{RETAIL_DATA_NOTE}</p>
            {moduleConfig?.notice ? <p>{moduleConfig.notice}</p> : null}
          </>
        }
      />

      <ApplicationFlowDiagram variant="data-query" />

      <DataQueryWorkspace examples={EXAMPLES} />
    </article>
  );
}
