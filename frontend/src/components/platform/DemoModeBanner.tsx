"use client";

/**
 * Demo 模式标识条。
 *
 * 它存在的唯一理由是**不让人误以为看到的是真实模型结果**。
 * 所以只做两件事：说清「当前是 Demo」，说清「没有调用真实模型」。
 * 不写「演示环境」「体验版」这类含糊说法——它们读起来像营销文案，
 * 起不到提醒作用。
 *
 * 真实模式下**完全不渲染**（不是渲染一个隐藏节点）：页面上不该出现
 * 任何「本可以显示 Demo 但这次没有」的痕迹。
 */

import { useEffect, useState } from "react";

import { fetchRuntimeMode, type RuntimeMode } from "@/lib/api/runtime";

import styles from "./DemoModeBanner.module.css";

export function DemoModeBanner() {
  const [mode, setMode] = useState<RuntimeMode | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    fetchRuntimeMode(controller.signal)
      .then(setMode)
      .catch(() => {
        // 取消是正常卸载，其它失败已经由客户端收敛成 null，这里无需再处理
      });

    return () => controller.abort();
  }, []);

  if (!mode?.demo) return null;

  return (
    <div className={styles.banner} role="status">
      <span className={styles.badge}>Demo 模式</span>
      <span className={styles.text}>
        当前页面展示的是<strong>内置样例数据与脚本化分析步骤</strong>，
        <strong>未调用任何真实模型</strong>，结果不可用于真实决策。
        切换到真实模式请把后端的 <code>APP_MODE</code> 设为 <code>real</code>。
      </span>
    </div>
  );
}
