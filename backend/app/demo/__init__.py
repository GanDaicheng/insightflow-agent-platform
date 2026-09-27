"""Demo 模式（APP_MODE=demo）的全部实现。

## 这个包存在的意义

Demo 模式的要求是「一个外部调用都不发」，而真实链路里发外部调用的地方有三条：
问数 Agent 的模型调用、RAG 的 embedding/精排/模型、经营分析的 Deep Agents。
把这三条各自的替身集中放在本包内，好处是：

- 想知道「demo 到底做了什么」，只需要读这一个目录；
- 真实链路的代码里只多出**三个**模式判断（三个装配点各一个），
  而不是几十个散落在业务逻辑里的 `if`。

## 边界

本包**只能被装配点导入**（`agent/data_query/graph.py`、
`services/rag_answer.py`、`services/business_analysis_runner.py`
和健康/模式接口）。业务节点、服务层不许依赖它——一旦 demo 的代码
渗进真实链路，真实模式就会开始受 demo 的改动影响。
"""

from app.demo.scenarios import (
    supported_questions,
)

__all__ = ["supported_questions"]
