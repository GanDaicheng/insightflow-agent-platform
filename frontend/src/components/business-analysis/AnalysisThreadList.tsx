import type { ReactNode } from "react";

import styles from "@/app/applications/business-analysis/business-analysis.module.css";

type Thread = { id: string; title: string; updatedAt: string };

type Props = {
  threads: Thread[];
  activeId: string;
  /** 正在生成时不允许切换会话——半路换走会把当前这次分析丢在后台没人看。 */
  busy?: boolean;
  onNew: () => void;
  onSelect: (thread: Thread) => void;
  onDelete: (thread: Thread) => void;
  footer?: ReactNode;
};

export function AnalysisThreadList({
  threads,
  activeId,
  busy = false,
  onNew,
  onSelect,
  onDelete,
  footer,
}: Props) {
  return (
    <aside className={styles.threadColumn} aria-label="分析会话">
      <div className={styles.threadHeader}>
        <span>分析会话</span>
        <button type="button" onClick={onNew}>＋ 新建</button>
      </div>
      {threads.length === 0 ? (
        <p className={styles.muted}>完成一次分析后，会话会显示在这里。</p>
      ) : (
        <ul className={styles.threadList}>
          {threads.map((thread) => (
            <li key={thread.id} className={styles.threadItem}>
              <button
                type="button"
                className={thread.id === activeId ? styles.activeThread : styles.thread}
                aria-current={thread.id === activeId ? "true" : undefined}
                disabled={busy}
                onClick={() => onSelect(thread)}
              >
                <span>{thread.title}</span>
                <small>{thread.updatedAt}</small>
              </button>
              <button
                type="button"
                className={styles.deleteThread}
                aria-label={`删除会话：${thread.title}`}
                title="删除会话"
                disabled={busy}
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete(thread);
                }}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {footer}
    </aside>
  );
}
