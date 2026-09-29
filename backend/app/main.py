import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.business_analysis_routes import router as business_analysis_router
from app.api.routes import router
from app.core.config import APP_MODE_DEMO, get_settings
from app.core.logging import configure_logging
from app.repositories.database import dispose_engine

configure_logging()

# 启动即校验配置。
#
# APP_MODE 这类值配错必须在**进程起不来**的时候就暴露，而不是等第一个请求
# 打进来才失败——后者会表现成「服务起来了但某个接口 500」，排查成本高得多。
# 校验失败时 Settings 构造抛异常，进程直接退出，这是刻意的。
_settings = get_settings()
if _settings.app_mode == APP_MODE_DEMO:
    logging.getLogger(__name__).warning(
        "APP_MODE=demo：模型、embedding、精排全部替换为确定性替身，"
        "所有回答都带 Demo 标识，不代表真实模型输出。"
    )

# 本地开发允许的前端来源。
#
# 为什么需要它：前端跑在 localhost:3000，后端在 localhost:8000，
# 端口不同就是跨域。没有这段配置，浏览器会在预检（OPTIONS）阶段
# 直接拦掉请求，页面连一个字节的响应都拿不到。
#
# 这里**逐个列出**来源，而不是用 ["*"]：
# 通配符等于允许任意站点带着浏览器里的凭据调用本服务。
# 同理只开放业务实际使用的 GET/POST/PUT/DELETE/OPTIONS，请求头只开 Content-Type。
# 这份名单是本地开发用的，生产环境应当由部署配置或受控的允许列表管理，
# 而不是写死在代码里。
DEVELOPMENT_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时什么都不用做，退出时释放数据库连接池。

    必须显式关闭，否则 --reload 反复重启会遗留一批没归还的连接，
    数据库端的连接数会慢慢涨上去。
    """
    yield
    await dispose_engine()


app = FastAPI(
    title="InsightFlow 数据智能 Agent 平台",
    description="面向零售与天猫数据的智能问数、知识检索和经营分析服务",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 只影响浏览器能否读到响应，不参与任何业务逻辑：
# 接口的行为、状态码、返回内容都不受它影响。
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEVELOPMENT_ALLOWED_ORIGINS,
    # 本服务不使用 Cookie / Authorization 凭据，保持 False 最小化暴露面
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(router)
app.include_router(business_analysis_router)
