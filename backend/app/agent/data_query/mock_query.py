"""模拟查询执行器：根据 intent 返回预定义的、可解释的固定结果。

⚠️ 这个模块**不是数据库执行器**，它跑不了任何 SQL，也不打算跑。
它做的是：看一眼意图，从四份写死的表格里挑一份返回。

**它现在不再是生产默认值。** 真实执行器是
query_execution.execute_real_query（走数据中台安全查询服务）。
本模块保留下来，专门用于三种场合：
- 单元测试；
- 没有数据库的环境下演示整条 Agent 流程；
- 需要精确构造结果的测试。

为什么 `execute_mock_query` 的参数里有个 `sql`，却完全不用它？
因为这个签名当初就是按**真实执行器**的形状定的：真实执行器必须拿到 SQL
才能查库。事后来看这个判断是对的——真实执行器接进来时，
execute_query 节点里那句 `await query_executor(sql=..., intent=...)` 一行都没改，
变的只是注进去的实现。

本模块不导入 sqlglot（不解析 SQL）、不导入数据库驱动、不读环境变量，
是纯粹的常量 + 纯函数。
"""

import copy

from app.agent.data_query.state import Intent, QueryResult

# 没有对应模拟数据时的返回值。
# 注意这是**正常业务结果**而不是错误：用户问了一个目录里没有的意图，
# 或者某个意图还没来得及造模拟数据，都不该让流程崩掉。
EMPTY_RESULT: QueryResult = {
    "columns": [],
    "rows": [],
    "row_count": 0,
    "source": "mock",
}


# 四类意图的固定模拟结果。
# 数值是编的，但必须符合基本业务常识，否则下游解释节点会说出荒唐的结论：
# - 销售额为正数，且月份之间有涨有跌（不是一条直线，否则「趋势」无从谈起）
# - 排名从 1 开始且不重复
# - 复购率落在 0~1 之间，且会员等级越高复购越强
# - row_count 必须等于 len(rows)
_MOCK_TREND: QueryResult = {
    "columns": ["month", "sales_amount"],
    "rows": [
        {"month": "2025-04", "sales_amount": 1286400.00},
        {"month": "2025-05", "sales_amount": 1341900.00},
        {"month": "2025-06", "sales_amount": 1512750.00},
        {"month": "2025-07", "sales_amount": 1438200.00},
        {"month": "2025-08", "sales_amount": 1623500.00},
        {"month": "2025-09", "sales_amount": 1709850.00},
    ],
    "row_count": 6,
    "source": "mock",
}

_MOCK_RANKING: QueryResult = {
    "columns": ["rank", "product_name", "sales_amount"],
    "rows": [
        {"rank": 1, "product_name": "智能空气净化器", "sales_amount": 486300.00},
        {"rank": 2, "product_name": "无线降噪耳机", "sales_amount": 412750.00},
        {"rank": 3, "product_name": "变频空调", "sales_amount": 358400.00},
        {"rank": 4, "product_name": "扫地机器人", "sales_amount": 301650.00},
        {"rank": 5, "product_name": "电动牙刷", "sales_amount": 245900.00},
    ],
    "row_count": 5,
    "source": "mock",
}

_MOCK_BREAKDOWN: QueryResult = {
    "columns": ["region_name", "sales_amount", "order_count"],
    "rows": [
        {"region_name": "华东", "sales_amount": 2860400.00, "order_count": 18420},
        {"region_name": "华南", "sales_amount": 1985600.00, "order_count": 12980},
        {"region_name": "华北", "sales_amount": 1642300.00, "order_count": 10450},
        {"region_name": "西南", "sales_amount": 987300.00, "order_count": 6320},
    ],
    "row_count": 4,
    "source": "mock",
}

_MOCK_REPURCHASE: QueryResult = {
    "columns": ["member_level", "customer_count", "repurchase_rate"],
    "rows": [
        {"member_level": "钻石", "customer_count": 1280, "repurchase_rate": 0.6234},
        {"member_level": "金卡", "customer_count": 5460, "repurchase_rate": 0.4812},
        {"member_level": "银卡", "customer_count": 14230, "repurchase_rate": 0.3156},
        {"member_level": "普通", "customer_count": 38650, "repurchase_rate": 0.1837},
    ],
    "row_count": 4,
    "source": "mock",
}

MOCK_RESULTS: dict[str, QueryResult] = {
    "trend": _MOCK_TREND,
    "ranking": _MOCK_RANKING,
    "breakdown": _MOCK_BREAKDOWN,
    "repurchase": _MOCK_REPURCHASE,
}


def execute_mock_query(*, sql: str, intent: Intent) -> QueryResult:
    """按意图返回固定的模拟结果。

    注意本函数**完全不读取 sql 参数**——不解析、不拼接、不执行，
    连它的长度都不看。它出现在签名里只是为了固定「执行器接收 SQL」
    这个接口方向，见模块顶部的说明。

    返回的是一份 deepcopy，不是内部常量本身。这一点必须做：
    QueryResult 里套着 list 和 dict，都是可变对象。如果直接把常量返回出去，
    调用方（或者某天某个下游节点）随手改一下 rows，就会**永久污染**
    MOCK_RESULTS ——之后所有请求拿到的都是被改过的数据，而且极难排查：
    代码看起来完全正常，数据却对不上。
    """
    template = MOCK_RESULTS.get(intent)
    if template is None:
        return copy.deepcopy(EMPTY_RESULT)
    return copy.deepcopy(template)


async def execute_mock_query_async(*, sql: str, intent: Intent) -> QueryResult:
    """execute_mock_query 的异步外壳，用来满足执行器的统一契约。

    为什么需要这一层？因为真实执行器必须 await（它要等数据库），
    节点的代码路径只有一条：
        result = await query_executor(sql=..., intent=...)
    如果 mock 执行器是同步的，节点就得写「先判断是不是协程」之类的分支，
    那才是真正的复杂度。让两个实现长得一样，节点才能对来源一无所知。

    真正的逻辑仍然只在 execute_mock_query 里，本函数不做任何加工。
    """
    return execute_mock_query(sql=sql, intent=intent)
