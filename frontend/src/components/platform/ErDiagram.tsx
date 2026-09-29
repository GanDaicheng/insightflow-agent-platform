import {
  RETAIL_DIMENSIONS,
  RETAIL_FACT,
  RETAIL_RELATIONS,
} from "@/features/platform/retail-tables";

import styles from "./ErDiagram.module.css";

type Props = {
  /** 是否展示下方的关系清单。默认展示——它是这张图不依赖颜色也能读懂的关键。 */
  showRelations?: boolean;
};

/**
 * 简化 ER 图：核心订单模型的四张维度表经外键汇入订单事实表。
 *
 * **关系不能只靠颜色和连线表达**：色觉障碍、黑白打印、或者缩到很窄的屏幕上，
 * 连线都有可能失效。所以每一条关联都在下面配一行文字（外键 → 主键 + 业务含义），
 * 图上那四条线只是让人一眼看出结构，真正的信息在文字里。
 *
 * 布局用 CSS Grid + 伪元素画连接线，不用 SVG：连线要在内容高度变化时自动跟着走，
 * 固定坐标的 SVG 反而要手算。窄屏时整块改成上下排列，连线和短横自动隐藏。
 */
export function ErDiagram({ showRelations = true }: Props) {
  return (
    <div className={styles.wrapper}>
      <div className={styles.diagram}>
        <ul className={styles.dims}>
          {RETAIL_DIMENSIONS.map((table) => (
            <li key={table.name} className={styles.dim}>
              <span className={styles.tableName}>{table.name}</span>
              <span className={styles.tableRole}>{table.role}</span>
            </li>
          ))}
        </ul>

        <div className={styles.brace} aria-hidden="true" />

        <div className={styles.fact}>
          <span className={styles.tableName}>{RETAIL_FACT.name}</span>
          <span className={styles.tableRole}>{RETAIL_FACT.role}</span>
          <span className={styles.factRows}>{RETAIL_FACT.rowsLabel}</span>
        </div>
      </div>

      <p className={styles.summary}>
        核心订单模型的四张维度表各自描述一个观察角度，订单事实表通过四个外键把它们串起来；渠道、库存、广告和售后表在下方表卡中展开。
      </p>

      {showRelations ? (
        <ul className={styles.relations}>
          {RETAIL_RELATIONS.map((relation) => (
            <li key={relation.from} className={styles.relation}>
              <code className={styles.relationPath}>
                {relation.from} → {relation.to}
              </code>
              <span className={styles.relationLabel}>{relation.label}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
