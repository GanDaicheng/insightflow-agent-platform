import type { Metadata } from "next";

import { DemoModeBanner } from "@/components/platform/DemoModeBanner";
import { PlatformShell } from "@/components/platform/PlatformShell";
import {
  PLATFORM_NAME,
  PLATFORM_TAGLINE,
} from "@/features/platform/platform-config";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: PLATFORM_NAME,
    template: `%s · ${PLATFORM_NAME}`,
  },
  description: `${PLATFORM_TAGLINE}。包含知识文档采集与向量检索、基于 LangGraph 的受控智能图表 Agent，以及知识问答与 AI 经营分析两个应用。`,
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body>
        {/* Demo 模式标识条。真实模式下它渲染成 null，页面上不留痕迹。 */}
        <DemoModeBanner />
        <PlatformShell>{children}</PlatformShell>
      </body>
    </html>
  );
}
