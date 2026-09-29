import type { Metadata } from "next";

import { PageHeader } from "@/components/ui/PageHeader";
import { ApplicationFlowDiagram } from "@/components/ui/ApplicationFlowDiagram";
import { DataQueryWorkspace } from "@/components/data-query/DataQueryWorkspace";
import { getModule, getSection, RETAIL_DATA_NOTE } from "@/features/platform/platform-config";

import { EXAMPLES } from "./examples";

const SECTION_ID = "applications";
const MODULE_SLUG = "data-query";

export const metadata: Metadata = {
  title: "自然语言智能图表 Agent",
  description: "用自然语言查询电商公司运营数据，自动生成图表和可核对的明细。",
};

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
        subtitle="把一个真实运营问题，直接变成一张可以核对的业务图表。"
        boundary={
          <>
            {/* 边界说明放在最顶上：用户提交的内容会被送到模型服务，
                这件事必须在动手输入之前就讲清楚 */}
            <p>
              当前为一家电商公司的内部运营样例数据，<strong>覆盖 2024—2026 年</strong>，
              包含订单、SKU、品类、省份、渠道、广告、库存、物流和售后数据。
              你可以从下面的真实运营问题开始，点击问题只会填入输入框，不会自动提交。
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
