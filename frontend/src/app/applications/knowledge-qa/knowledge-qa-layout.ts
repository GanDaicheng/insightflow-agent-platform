const KNOWLEDGE_QA_SETTINGS_WIDTH = 240;

/** 桌面端主输入区 + 紧凑检索设置栏的列定义。 */
export function getKnowledgeQaGridTemplate() {
  return `minmax(0, 1fr) ${KNOWLEDGE_QA_SETTINGS_WIDTH}px`;
}
