"""ORM 模型包。

Alembic 的 env.py 只导入本包，因此新增模型后必须在这里登记一次，
否则 autogenerate 会「看不见」这张表，迁移文件里就会缺表。
"""

from app.models.base import Base
from app.models.knowledge import (
    KNOWLEDGE_EMBEDDING_DIMENSIONS,
    KnowledgeChunk,
    KnowledgeDocument,
    Vector,
)
from app.models.retail import (
    MEMBER_LEVELS,
    AdCampaign,
    AdDailyMetric,
    AfterSale,
    Channel,
    Customer,
    DateDim,
    InventorySnapshot,
    Order,
    OrderOperation,
    Product,
    Promotion,
    Region,
)
from app.models.tmall import (
    GOLD_TABLES,
    INGESTION_STATUSES,
    LABEL_GROUPS,
    SILVER_TABLES,
    TmallCategoryMetric,
    TmallDailyMetric,
    TmallFunnelMetric,
    TmallIngestionRun,
    TmallMerchantMetric,
    TmallRepurchaseMetric,
    TmallRepurchaseSample,
    TmallUser,
    TmallUserEvent,
    TmallUserMetric,
)

__all__ = [
    "GOLD_TABLES",
    "INGESTION_STATUSES",
    "KNOWLEDGE_EMBEDDING_DIMENSIONS",
    "LABEL_GROUPS",
    "MEMBER_LEVELS",
    "SILVER_TABLES",
    "Base",
    "AdCampaign",
    "AdDailyMetric",
    "AfterSale",
    "Channel",
    "Customer",
    "DateDim",
    "InventorySnapshot",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "Order",
    "OrderOperation",
    "Product",
    "Promotion",
    "Region",
    "TmallCategoryMetric",
    "TmallDailyMetric",
    "TmallFunnelMetric",
    "TmallIngestionRun",
    "TmallMerchantMetric",
    "TmallRepurchaseMetric",
    "TmallRepurchaseSample",
    "TmallUser",
    "TmallUserEvent",
    "TmallUserMetric",
    "Vector",
]
