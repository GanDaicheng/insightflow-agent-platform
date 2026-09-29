"use client";

import type { CSSProperties, KeyboardEvent } from "react";

import { Button } from "@/components/ui/Button";
import {
  RAG_DEFAULT_TOP_K,
  RAG_MAX_TOP_K,
  RAG_MIN_TOP_K,
  RAG_QUESTION_MAX_LENGTH,
} from "@/lib/api/rag-answer";

import styles from "./knowledge-qa.module.css";
import { getKnowledgeQaGridTemplate } from "./knowledge-qa-layout";

type Props = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onClear: () => void;
  /** 请求进行中：输入框与所有按钮都不可用，避免重复提交。 */
  busy: boolean;
  topK: number;
  onTopKChange: (value: number) => void;
  examples: readonly ExampleGroup[];
};

export type ExampleGroup = {
  label: string;
  items: readonly string[];
};

/** top_k 的可选值。用下拉而不是数字输入框：不给用户输入非法值的机会。 */
const TOP_K_CHOICES = Array.from(
  { length: RAG_MAX_TOP_K - RAG_MIN_TOP_K + 1 },
  (_, index) => RAG_MIN_TOP_K + index,
);

/**
 * 问题输入区。
 *
 * 纯受控组件：自己不持有状态、不发请求，只把用户动作回调出去。
 * 这样「提交后会发生什么」全部集中在工作台组件里，输入区只管道具。
 */
export function KnowledgeQuestionInput({
  value,
  onChange,
  onSubmit,
  onClear,
  busy,
  topK,
  onTopKChange,
  examples,
}: Props) {
  const trimmed = value.trim();
  const tooLong = value.length > RAG_QUESTION_MAX_LENGTH;
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
    <section className={styles.card} aria-labelledby="kq-question-heading">
      <h2 id="kq-question-heading" className={styles.cardTitle}>
        输入问题
      </h2>
      <p className={styles.cardCaption}>
        用一句中文问业务口径、指标定义、规则说明或数据字典。例如「客单价怎么算」。
      </p>

      <div
        className={styles.questionLayout}
        style={{ "--kq-grid-columns": getKnowledgeQaGridTemplate() } as CSSProperties}
      >
        <div className={styles.questionMain}>
          <label className={styles.hint} htmlFor="kq-question-input">
            知识类问题
          </label>
          <textarea
            id="kq-question-input"
            className={styles.textarea}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={busy}
            placeholder="例如：客单价怎么算？"
            aria-describedby="kq-question-meta"
            // 刻意不设 maxLength：要给用户「超长时看到提示」的机会，
            // 直接截断会让人以为自己输全了
          />

          <div className={styles.inputMeta} id="kq-question-meta">
            <span className={styles.counter} data-over={tooLong}>
              已输入 {value.length} / {RAG_QUESTION_MAX_LENGTH} 字
            </span>
            <span className={styles.hint}>Ctrl / Cmd + Enter 也可以提交</span>
          </div>

          {tooLong ? (
            <p className={styles.validation} role="alert">
              问题不能超过 {RAG_QUESTION_MAX_LENGTH} 字，请精简后再提交。
            </p>
          ) : null}

          <div className={styles.actions}>
            <Button variant="primary" onClick={onSubmit} disabled={!canSubmit}>
              {busy ? "检索中…" : "提问"}
            </Button>
            <Button
              variant="secondary"
              onClick={onClear}
              disabled={busy || (value.length === 0 && trimmed.length === 0)}
            >
              清空
            </Button>
          </div>
        </div>

        <aside className={styles.options} aria-label="检索设置">
          <label className={styles.hint} htmlFor="kq-top-k">
            检索资料条数
          </label>
          <select
            id="kq-top-k"
            className={styles.select}
            value={topK}
            onChange={(event) => onTopKChange(Number(event.target.value))}
            disabled={busy}
          >
            {TOP_K_CHOICES.map((choice) => (
              <option key={choice} value={choice}>
                {choice} 条
                {choice === RAG_DEFAULT_TOP_K ? "（默认）" : ""}
              </option>
            ))}
          </select>
          <p className={styles.optionNote}>
            条数越多，作为回答依据的资料越全，但模型要读的内容也越多。资料重复时，加大条数未必更好。
          </p>
        </aside>
      </div>

      <div className={styles.examples}>
        <p className={styles.examplesLabel}>示例问题（点击只填入输入框，不会自动提交）</p>
        <div className={styles.exampleGroups}>
          {examples.map((group) => (
            <section key={group.label} className={styles.exampleGroup} aria-labelledby={`kq-example-${group.label}`}>
              <h3 id={`kq-example-${group.label}`} className={styles.exampleGroupTitle}>{group.label}</h3>
              <ul className={styles.exampleList}>
                {group.items.map((example) => (
                  <li key={example}>
                    <button type="button" className={styles.example} onClick={() => onChange(example)} disabled={busy}>
                      {example}
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
