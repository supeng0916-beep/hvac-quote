# 界面预览来源

README 包含四张实际应用页面截图，以及两张实际导出文件预览，制作日期为 2026-10-26。

| 文件 | 内容 | 拍摄方式 |
| --- | --- | --- |
| `01-workspace.jpg` | 项目总览 | 全页截图 |
| `02-inspection.jpg` | 图纸检查 | 全页截图 |
| `03-confirmation.jpg` | 设备与管线确认 | 页面滚动至设备组后的可视区域 |
| `04-quotation.jpg` | 报价结果与范围 | 全页截图 |
| `05-pdf-export.png` | 导出的 PDF 报价单第一页 | Poppler 直接渲染原 PDF，长边 2000 像素 |
| `06-excel-export.jpg` | 导出的 Excel 报价明细 | LibreOffice Calc 窗口真实截图 |

复现步骤：

1. 运行 `uv run python -m hvac_quote.demo --out data/demo`。
2. 运行 `uv run hvac-quote serve --port 8765 --data data/demo/data --runs data/demo/runs`。
3. 使用生成器输出的随机密码登录 `demo` 账号。
4. 从项目总览进入项目，查看图纸检查、已有草稿与报价结果，再用浏览器截图。
5. 用 LibreOffice Calc 打开导出的 `quote.xlsx`，截取窗口；不要修改或保存工作簿。
6. 对导出的 `quote.pdf` 运行 `pdftoppm -f 1 -singlefile -scale-to 2000 -png quote.pdf 05-pdf-export`，直接渲染第一页，不重新排版或重建表格。

数据来自生成器创建的虚构办公空间。演示账号、工程图、价格表均为新建；截图未使用原客户项目，未修改 DOM，未拼接或绘制界面。日期、内部记录 ID 和随机密码随运行变化。不要把生成器输出的 `preview.json` 或数据库放入公开仓库。
