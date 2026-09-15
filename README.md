# Nasdaq Add Dashboard

这是加仓仪表盘的 GitHub Pages 公网静态展示版。它发布一组经过本地服务生成的只读快照，保留总览、回撤触发、周期研究、资金管理、证据核验、指标说明和评分账本页面。

GitHub Pages 不运行本项目的 Python API，因此公网版本不会执行联网更新、原始证据导入或写入数据库。页面会明确显示这一边界。需要刷新数据时，在完整的本地项目目录启动 `Start.command`，执行 `python3 build_static_data.py`，再把生成的 `static-data.json` 与页面文件一起发布。本公开仓库只保存静态发布文件，不包含本地数据库和 Python 服务。

价格、评分和证据仍遵守项目的时点边界；静态快照的生成日期会显示在页面中。

## 免费联网后端

仓库现在附带 `backend/` 和根目录 `render.yaml`。用 Render Free 创建 Blueprint 后，后端会与页面同源运行，`联网更新` 会在云端执行真实来源采集并显示阶段进度；冷启动时先读取已发布的脱敏快照。部署步骤和免费计划的临时磁盘限制见 [`DEPLOY_FREE_BACKEND.md`](DEPLOY_FREE_BACKEND.md)。
