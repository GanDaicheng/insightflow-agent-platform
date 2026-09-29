# 历史天猫数据域

这里保存项目早期使用的天猫 IJCAI 2015 数据域，仅用于历史追溯，不属于当前产品运行路径。

- 默认 Demo 不读取这里的代码、数据或知识文档。
- `pytest` 不会收集这里的历史测试。
- Docker 初始化不会下载或导入天猫原始数据。
- 已发布的 Alembic 天猫迁移仍保留在 `backend/alembic/versions/`，不改写历史迁移，也不会自动删除已有数据库表。

当前项目的主数据域是电商公司经营数仓，使用 `backend/scripts/seed_retail_demo_data.py` 生成合成演示数据。
