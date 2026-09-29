import type { AgentDataQueryResponse } from "@/lib/api/agent-data-query";
import { Notice } from "@/components/ui/Notice";

import { AgentEventTimeline } from "./AgentEventTimeline";
import { ResultChart } from "./ResultChart";
import { ResultTable } from "./ResultTable";
import styles from "./data-query.module.css";

type Props = {
  result: AgentDataQueryResponse;
};

/**
 * 执行记录。
 *
 * 默认折叠：它是「想核对时才会看」的第三层信息，摊开会把结论和图表往下推。
 * 摘要里写上步数，折叠状态下也能知道里面有多少内容。
 *
 * 用原生 <details> 而不是自己写展开状态：键盘操作、屏幕阅读器的展开/折叠语义
 * 浏览器已经做对了，自己实现一遍只会漏掉这些。
 */
function ExecutionLog({ events }: { events: string[] }) {
  if (events.length === 0) return null;

  return (
    <details className={styles.log}>
      <summary className={styles.logSummary}>
        本次执行记录（{events.length} 步）
      </summary>
      <p className={styles.logNote}>
        以下是后端返回的公开步骤，前端不做推断或补充。
      </p>
      <AgentEventTimeline events={events} />
    </details>
  );
}

/**
 * 结果区。
 *
 * 两种形态泾渭分明：
 * - status === "error"：只展示受控的错误说明和执行记录，**不展示结果与图表**。
 *   出错时 State 里可能残留上一次的中间产物，展示出来就是误导。
 * - status === "ok"：按「图表 → 关键结论 → 明细 → 执行记录 → 知识资料」
 *   的优先级排。智能问数首先服务于看数据和做图，经营解释交给 AI 经营分析。
 *   没有查询结果也是正常结果（例如问题不属于问数范畴），照实说明即可，不当成故障。
 */
export function QueryResultPanel({ result }: Props) {
  if (result.status === "error") {
    return (
      <section className={styles.card} aria-labelledby="result-error-heading">
        <h2 id="result-error-heading" className={styles.cardTitle}>
          本次分析未完成
        </h2>
        <p className={styles.cardCaption}>
          Agent 返回了受控的失败说明，因此没有查询结果，也没有图表建议。
        </p>
        <p className={`${styles.answer} ${styles.answerError}`}>{result.answer}</p>
        <ExecutionLog events={result.events} />
      </section>
    );
  }

  const queryResult = result.query_result;
  const suggestion = result.chart_suggestion;
  // 度量字段与它的展示格式都来自图表建议；表格只对这一列套格式
  const valueField = suggestion?.y_field ?? null;
  const valueFields = suggestion?.y_fields?.length
    ? suggestion.y_fields
    : valueField
      ? [valueField]
      : [];
  const valueFormat = suggestion?.value_format ?? null;
  // 后端老版本没有这个字段，缺席时按空数组处理（那一块就不渲染）
  const knowledgeSources = result.knowledge_sources ?? [];

  return (
    <div className={styles.page}>
      {/* 1. 图表或表格：智能问数的主输出 */}
      {queryResult ? (
        <>
          <section className={styles.card} aria-labelledby="chart-heading">
            <h2 id="chart-heading" className={styles.cardTitle}>
              图表建议
            </h2>
            <ResultChart result={queryResult} suggestion={suggestion} />
          </section>

          {/* 2. 数据来源与结果行数 —— 图表的可核对底稿 */}
          <section className={styles.card} aria-labelledby="table-heading">
            <h2 id="table-heading" className={styles.cardTitle}>
              查询结果
            </h2>
            <ResultTable
              result={queryResult}
              valueField={valueField}
              valueFields={valueFields}
              valueFormat={valueFormat}
            />
          </section>
        </>
      ) : (
        <Notice tone="neutral" tag="无结果">
          本次没有返回查询结果。这通常意味着问题不属于数据分析范畴，
          或者没有匹配到可用的数据资产——两种情况都不是系统故障。
          注意「没有结果」不等于「结果为 0」。
        </Notice>
      )}

      {/* 3. 关键结论：只复述查询结果，不承担多步骤原因分析 */}
      <section className={styles.card} aria-labelledby="answer-heading">
        <h2 id="answer-heading" className={styles.cardTitle}>
          关键结论
        </h2>
        <p className={styles.cardCaption}>
          以下内容只根据本次查询结果生成；需要继续追查原因和建议时，请使用 AI 经营分析。
        </p>
        <p className={styles.answer}>{result.answer}</p>
      </section>

      {/* 4. 执行记录（可折叠）。没有步骤时整块不渲染，不留一张空卡片 */}
      {result.events.length > 0 ? (
        <section className={styles.card}>
          <ExecutionLog events={result.events} />
        </section>
      ) : null}

      {/* 5. 知识参考资料。放在最后：它是解释的依据，不是数字的来源。
          没有查知识库（或后端没返回这个字段）时整块不渲染，不留空框。 */}
      {knowledgeSources.length > 0 ? (
        <section className={styles.card} aria-labelledby="knowledge-heading">
          <h2 id="knowledge-heading" className={styles.cardTitle}>
            检索参考资料
          </h2>
          {/* 这条边界必须写在用户看得到的地方：数字来自查询结果，
              文档只解释口径与原因。不写清楚，用户会以为数字出自这些文档。 */}
          <p className={styles.cardCaption}>
            本次回答参考的业务文档小节。数据结论来自查询结果，
            这些资料只用于解释口径与可能的原因，且不代表模型逐条引用过。
          </p>
          <ul className={styles.knowledgeList}>
            {knowledgeSources.map((source, index) => (
              <li
                key={`${source.source_file}-${source.section_title}-${index}`}
                className={styles.knowledgeItem}
              >
                <div className={styles.knowledgeHead}>
                  <span className={styles.knowledgeRank}>#{index + 1}</span>
                  <span className={styles.knowledgeSection}>{source.section_title}</span>
                  <span className={styles.knowledgeDoc}>{source.document_title}</span>
                  <span
                    className={styles.knowledgeSimilarity}
                    title="相似度只用于比较同一批结果的相对好坏，不是答案正确率"
                  >
                    相似度 {source.similarity.toFixed(4)}
                  </span>
                </div>
                {source.preview ? (
                  <p className={styles.knowledgePreview} title="命中的文档原文（已截断）">
                    {source.preview}
                  </p>
                ) : null}
                <span className={styles.knowledgeFile}>{source.source_file}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
