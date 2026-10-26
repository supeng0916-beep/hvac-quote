# 界面预览来源

README 的四张 PNG 来自本地启动的 HVAC Quote 页面，拍摄日期为 2026-10-26。

| 文件 | 内容 | 拍摄方式 |
| --- | --- | --- |
| `01-workspace.png` | 项目总览 | 全页截图 |
| `02-inspection.png` | 图纸检查 | 全页截图 |
| `03-confirmation.png` | 设备与管线确认 | 页面滚动至设备组后的可视区域 |
| `04-quotation.png` | 报价结果与范围 | 全页截图 |

复现步骤：

1. 运行 `uv run python -m hvac_quote.demo --out data/demo`。
2. 运行 `uv run hvac-quote serve --port 8765 --data data/demo/data --runs data/demo/runs`。
3. 使用生成器输出的随机密码登录 `demo` 账号。
4. 从项目总览进入项目，查看图纸检查、已有草稿与报价结果，再用浏览器截图。

数据来自生成器创建的虚构办公空间。演示账号、工程图、价格表均为新建；截图未使用原客户项目，未修改 DOM，未拼接或绘制界面。日期、内部记录 ID 和随机密码随运行变化。不要把生成器输出的 `preview.json` 或数据库放入公开仓库。
