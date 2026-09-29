import type { Metadata } from "next";

import { PageHeader } from "@/components/ui/PageHeader";
import { ApplicationFlowDiagram } from "@/components/ui/ApplicationFlowDiagram";
import { getSection, RETAIL_DATA_NOTE } from "@/features/platform/platform-config";

import { KnowledgeQaWorkspace } from "./KnowledgeQaWorkspace";
import { KNOWLEDGE_QA_EXAMPLES } from "./examples";

const SECTION_ID = "applications";

export const metadata: Metadata = {
  title: "RAG知识问答",
  description: "基于零售知识库回答问题，给出回答并列出检索到的参考资料。",
};

/**
 * 示例问题。
 *
 * 全部取自知识库文档里确实写了答案的小节，覆盖 5 份文档各一次——
 * 点进去就能看到一次成功的回答，而不是先撞上「资料不足」。
 * 这也让页面本身成了一条可演示的验收路径。
 *
 * 定义在服务端组件里、以 props 传给客户端工作台——数组是可序列化的，
 * 不会把整份配置或函数带进客户端包。
 *
 * 点击示例只填入输入框，不自动提交：每次提问都会真实调用 embedding 与模型服务，
 * 不能因为一次误点就花掉两次调用。
 */
/**
 * 知识库问答页面。
 *
 * 路由文件刻意保持很薄：只负责页头、边界说明和示例清单这些静态内容，
 * 交互与取数全部交给 KnowledgeQaWorkspace（客户端组件）。
 * 这样绝大部分内容仍然是服务端渲染的。
 *
 * 边界说明里刻意**不写**文档份数与切片数：那是会变的，
 * 写死在这里就等于制造一个没人会同步的副本。真实清单在「数据采集」页，
 * 由接口返回、按实际数据计算。
 */
export default function Page() {
  const section = getSection(SECTION_ID);

  return (
    <article>
      <PageHeader
        sectionName={section?.name ?? "智能应用"}
        title="RAG知识问答"
        align="center"
        subtitle="基于零售知识库回答问题：业务口径、指标定义、规则说明与数据字典。回答只依据知识库文档，并列出检索到的参考资料。"
        boundary={
          <>
            <p>
              当前为本地零售样例知识库。提交问题会调用 embedding 与模型服务，
              不适合输入敏感信息。知识库里到底有哪些文档，以
              「数据采集」页列出的清单为准。
            </p>
            <p>{RETAIL_DATA_NOTE}</p>
            <p>
              回答只依据知识库中已有的文档。资料不足时会明确说明「没有足够信息」，
              不会用模型自身的知识补全。
            </p>
            <p>
              这是一条独立链路：它只查知识库，不查业务数据。同层的「智能问数」
              查的是业务数据，并在需要解释口径时引用知识库。
            </p>
          </>
        }
      />

      <ApplicationFlowDiagram variant="rag" />

      <KnowledgeQaWorkspace examples={KNOWLEDGE_QA_EXAMPLES} />
    </article>
  );
}
