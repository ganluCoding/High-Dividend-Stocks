# 数据补齐进度桌面页

现有 `高股息研究.app` 已增加轻量进度监视页：

- 启动应用后默认进入“数据补齐进度”；
- 启动时立即读取本机最新发布包；
- 每 5 分钟自动刷新一次；
- 支持“立即刷新”；
- 展示发布版本、最后刷新时间、各数据集已采集数量、进度条和平均完整度；
- 只读取已通过发布门禁的SQLite快照，不会触发数据抓取，也不会连接券商或自动交易。

安装位置：`~/Applications/高股息研究.app`。

重新构建/安装：

```bash
cd client
npm run tauri -- build --debug
cd ..
python3 scripts/launch_desktop_client.py --install
```
