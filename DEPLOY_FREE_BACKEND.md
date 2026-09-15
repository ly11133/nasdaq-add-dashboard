# 免费后端部署

本仓库同时包含 GitHub Pages 静态展示版和一个可部署到 Render Free 的 Python 后端。`render.yaml` 将服务根目录设为 `backend/`，其中只带价格种子与已脱敏静态快照，不包含本机 SQLite、备份或原始采集缓存。

## 一键部署

1. 打开 <https://dashboard.render.com/blueprints>，选择 **New Blueprint Instance**。
2. 连接 GitHub 仓库 `ly11133/nasdaq-add-dashboard`，选择 `render.yaml`。
3. 保持 `nasdaq-add-dashboard-backend` 为 Free，点击创建。
4. 创建完成后打开 Render 分配的 `https://…onrender.com/` 地址；页面与 `/api/status` 在同一域名，点击“联网更新”会在云端执行采集。

也可以直接打开仓库专用入口：<https://render.com/deploy?repo=https://github.com/ly11133/nasdaq-add-dashboard>。首次连接 GitHub/Render 账号仍需要用户本人完成授权。

## 免费计划边界

Render Free 服务会在空闲一段时间后休眠，唤醒有冷启动延迟；实例文件系统是临时的，重启或重新部署后本次运行产生的 SQLite/缓存会丢失。因此仓库内的 `static-data.json` 是冷启动恢复点，联网刷新后的新快照只在当前实例存活期间保留。要跨重启保存历史，需要付费持久磁盘或外部数据库。

更新接口使用真实阶段进度并后台执行，来源请求仍遵守原有 PIT/代理标签；免费实例不改变“未知不计分”和“不能把当前历史回填为过去证据”的规则。
