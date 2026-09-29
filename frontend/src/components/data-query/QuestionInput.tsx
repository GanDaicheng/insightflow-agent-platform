import type { KeyboardEvent } from "react";

import { Button } from "@/components/ui/Button";
import { QUESTION_MAX_LENGTH } from "@/lib/api/agent-data-query";

import type { ExampleGroup, ExampleItem } from "../../app/applications/data-query/examples";
import styles from "./data-query.module.css";

type Props = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onClear: () => void;
  /** 请求进行中：输入框与所有按钮都不可用，避免重复提交。 */
  busy: boolean;
  examples: readonly ExampleGroup[];
};

/**
 * 问题输入区。
 *
 * 纯受控组件：自己不持有状态、不发请求，只把用户动作回调出去。
 * 这样「提交后会发生什么」全部集中在工作台组件里，输入区只管道具。
 */
export function QuestionInput({
  value,
  onChange,
  onSubmit,
  onClear,
  busy,
  examples,
}: Props) {
  const trimmed = value.trim();
  const tooLong = value.length > QUESTION_MAX_LENGTH;
  const canSubmit = !busy && trimmed.length > 0 && !tooLong;

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Ctrl/Cmd + Enter 提交。这是多行输入框的通用约定：
    // 单按 Enter 要留给换行，不能抢。
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (canSubmit) onSubmit();
    }
  }

  return (
    <section className={styles.card} aria-labelledby="question-heading">
      <h2 id="question-heading" className={styles.cardTitle}>
        你现在最想看清哪个运营问题？
      </h2>
      <p className={styles.cardCaption}>
        用一句中文描述目标，Agent 会选择数据工具，优先生成图表，并保留明细作为核对底稿。
      </p>

      <div className={styles.promise} aria-label="问数结果说明">
        <div>
          <span className={styles.promiseKicker}>你会得到</span>
          <strong>一张可解释的运营图表</strong>
        </div>
        <span className={styles.promiseArrow} aria-hidden="true">→</span>
        <div>
          <span className={styles.promiseKicker}>同时保留</span>
          <strong>数据明细与查询依据</strong>
        </div>
      </div>

      <label className={styles.hint} htmlFor="question-input">
        自然语言问题
      </label>
      <textarea
        id="question-input"
        className={styles.textarea}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        disabled={busy}
        placeholder="例如：2025 年各月销售额趋势怎么样？"
        aria-describedby="question-meta"
        // 刻意不设 maxLength：要给用户「超长时看到提示」的机会，
        // 直接截断会让人以为自己输全了
      />

      <div className={styles.inputMeta} id="question-meta">
        <span className={styles.counter} data-over={tooLong}>
          已输入 {value.length} / {QUESTION_MAX_LENGTH} 字
        </span>
        <span className={styles.hint}>Ctrl / Cmd + Enter 也可以提交</span>
      </div>

      {tooLong ? (
        <p className={styles.validation} role="alert">
          问题不能超过 {QUESTION_MAX_LENGTH} 字，请精简后再提交。
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button variant="primary" onClick={onSubmit} disabled={!canSubmit}>
          {busy ? "分析中…" : "开始分析"}
        </Button>
        <Button
          variant="secondary"
          onClick={onClear}
          disabled={busy || (value.length === 0 && trimmed.length === 0)}
        >
          清空
        </Button>
      </div>

      <div className={styles.examples}>
        <div className={styles.examplesIntro}>
          <p className={styles.examplesLabel}>从一个真实的运营动作开始</p>
          <span>点击问题只填入输入框，不会自动提交</span>
        </div>
        <div className={styles.exampleGroups}>
          {examples.map((group) => (
            <section key={group.label} className={styles.exampleGroup} aria-labelledby={`dq-example-${group.label}`}>
              <div className={styles.exampleGroupHead}>
                <div>
                  <h3 id={`dq-example-${group.label}`} className={styles.exampleGroupTitle}>{group.label}</h3>
                  <p className={styles.exampleGroupDescription}>{group.description}</p>
                </div>
                <span className={styles.groupIndex}>{String(examples.indexOf(group) + 1).padStart(2, "0")}</span>
              </div>
              <ul className={styles.exampleList}>
                {group.items.map((example: ExampleItem) => (
                  <li key={example.id}>
                    <button type="button" className={styles.example} onClick={() => onChange(example.question)} disabled={busy}>
                      <span className={styles.exampleQuestion}>{example.question}</span>
                      <span className={styles.exampleMeta}>
                        <span>{example.context}</span>
                        <span className={styles.exampleChart} data-chart={example.chartLabel}>{example.chartLabel}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </section>
  );
}
