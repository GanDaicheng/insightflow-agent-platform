import type { Metadata } from "next";

import { ErDiagram } from "@/components/platform/ErDiagram";
import { LinkButton } from "@/components/ui/Button";
import { Notice } from "@/components/ui/Notice";
import { PageHeader } from "@/components/ui/PageHeader";
import {
  PAGE_HREFS,
  RETAIL_DATA_NOTE,
  getModule,
  getSection,
} from "@/features/platform/platform-config";
import { RETAIL_TABLES } from "@/features/platform/retail-tables";

import styles from "./warehouse.module.css";

const SECTION_ID = "data";
const MODULE_SLUG = "warehouse";

export const metadata: Metadata = {
  title: "数据仓库",
  description:
    "PostgreSQL 中的电商公司运营数据底座：订单、SKU、渠道、广告、库存和售后数据，为智能图表和经营分析提供基础。",
};

/**
 * 数据仓库页面。
 *
 * 这一页回答的是「智能问数到底查的是什么」。没有它，问数页上的
 * 「查询真实 PostgreSQL 样例数据」就只是一句话，看不到数据长什么样。
 *
 * 数字全部来自 features/platform/retail-tables.ts 的静态配置（实测值），
 * 不是实时统计——所以页面上有明确的「演示数据」标注，
 * 而且**没有**去做一个假的「实时表行数」接口。
 */
export default function Page() {
  const section = getSection(SECTION_ID);
  const moduleConfig = getModule(SECTION_ID, MODULE_SLUG);

  return (
    <article>
      <PageHeader
        sectionName={section?.name ?? "数据中台"}
        title="数据仓库"
        subtitle="PostgreSQL 中的电商公司运营数据底座。核心订单星型模型连接客户、SKU、省份和日期，并扩展渠道、促销、广告、库存、物流和售后事实，智能图表和经营分析都从这些表取数。"
        boundary={
          <>
            <p>{RETAIL_DATA_NOTE}</p>
            {moduleConfig?.notice ? <p>{moduleConfig.notice}</p> : null}
          </>
        }
        boundaryTone="warning"
        boundaryLabel="数据说明"
      />

      <section className={styles.block}>
        <h2 className={styles.blockTitle}>PostgreSQL 数据底座</h2>
        <p className={styles.blockText}>
          平台用 PostgreSQL 16 存放业务数据，并启用了 pgvector 扩展。
          业务数据与知识库向量放在同一个数据库里，但用途完全不同：
          <strong>业务数据走 SQL 聚合计算，知识库走向量相似度检索</strong>。
        </p>
        <p className={styles.blockText}>
          事实表只保存订单明细的原始粒度，不存任何预聚合结果——
          月度、省份、SKU、渠道和品类的汇总全部由 SQL 现场算出来，
          这样数据才算「可真实分析」，而不是读一份别人算好的答案。
        </p>
      </section>

      <section className={styles.block}>
        <div className={styles.blockHead}>
          <h2 className={styles.blockTitle}>12 张运营样例表</h2>
          <p className={styles.blockNote}>
            行数为实测值，写在前端静态配置里，不是实时统计。
          </p>
        </div>

        <ul className={styles.tableGrid}>
          {RETAIL_TABLES.map((table) => (
            <li
              key={table.name}
              className={styles.tableCard}
              data-kind={table.kind}
            >
              <div className={styles.tableHead}>
                <span className={styles.tableName}>{table.name}</span>
                <span className={styles.tableKind}>
                  {table.kind === "fact" ? "事实表" : "维度表"}
                </span>
              </div>
              <p className={styles.tableRole}>{table.role}</p>
              <p className={styles.tableRows}>{table.rowsLabel}</p>

              <ul className={styles.fieldList}>
                {table.fields.map((field) => (
                  <li key={field.name} className={styles.fieldItem}>
                    <code className={styles.fieldName}>{field.name}</code>
                    <span className={styles.fieldDesc}>{field.desc}</span>
                  </li>
                ))}
              </ul>

              <p className={styles.tablePurpose}>{table.purpose}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className={styles.block}>
        <div className={styles.blockHead}>
          <h2 className={styles.blockTitle}>表之间的关系</h2>
          <p className={styles.blockNote}>
            核心订单模型通过外键连接客户、SKU、省份和日期；运营事实表继续关联渠道、促销、广告、库存和售后。
          </p>
        </div>

        <ErDiagram />
      </section>

      <section className={styles.block}>
        <h2 className={styles.blockTitle}>用这些数据能做什么</h2>
        <p className={styles.blockText}>
          口径与规则类问题（「客单价怎么算」）去知识问答；
          数值类问题（「2025 年每月销售额和毛利趋势」）去智能图表，由 Agent 生成 SQL 在真实数据上计算。
        </p>

        <div className={styles.actions}>
          <LinkButton href={PAGE_HREFS.knowledgeQa} variant="primary">
            基于数据口径提问
          </LinkButton>
          <LinkButton href={PAGE_HREFS.dataQuery}>
            使用智能图表
          </LinkButton>
        </div>
      </section>

      <div className={styles.noticeWrap}>
        <Notice tone="info" tag="演示数据">
          {RETAIL_DATA_NOTE}
          这 12 张表是为把「自然语言取数 → 图表 → 经营分析」链路真正跑通而准备的样例数据，
          不代表真实生产数据，也没有声称已经完成 ODS / DWD / DWS / ADS 分层建模。
        </Notice>
      </div>
    </article>
  );
}
