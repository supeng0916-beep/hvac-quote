# 运行指南

## 系统依赖

| 依赖 | 用途 | 说明 |
| --- | --- | --- |
| Python 3.12+、uv | 应用与依赖管理 | `uv sync --frozen` 安装开发依赖和演示所需的 HTTP 测试客户端 |
| LibreOffice | Excel 公式重算与金额核对 | `soffice` 应在 PATH 中；macOS 也自动检查默认应用路径 |
| 中文字体 | PDF 中文渲染 | macOS 可使用系统宋体；Linux 建议 `fonts-wqy-microhei` |
| LibreDWG | DWG 转 DXF | `dwg2dxf` 应在 PATH 中；DXF 输入不需要它 |

macOS 可安装 LibreOffice 应用，或使用 `brew install --cask libreoffice`；需要 DWG 时安装 `brew install libredwg`。Debian/Ubuntu 的导出依赖可安装 `libreoffice-calc fonts-wqy-microhei`。容器构建文件包含这些依赖和 LibreDWG 0.14。

## Docker

在仓库根目录执行：

```bash
docker compose up --build -d
docker compose logs -f
```

打开 `http://127.0.0.1:8000`。默认只绑定本机，运行数据保存在 `./data`。首次构建需要下载系统和 Python 依赖，并编译 DWG 转换器；仓库没有要求使用任何私有镜像。

停止服务用 `docker compose down`，不会删除挂载的数据目录。需要修改本机端口时，设置 `HVAC_PORT`；如确需局域网共享，可设置 `HVAC_BIND=0.0.0.0`，同时按部署环境配置访问限制。

## 账号和数据

首次启动通过网页建立管理员账号，也可在本机交互式创建账号：

```bash
uv run hvac-quote user-add --username admin --display-name 管理员 --data data
uv run hvac-quote user-list --data data
```

密码在提示时输入，不必写入命令行。不同启动命令应使用同一组 `--data`、`--runs` 路径；演示目录与日常目录应分开。

数据库备份：

```bash
uv run hvac-quote backup --data data
```

这条命令只备份数据库。完整恢复还需要上传资料和报价输出，因此应在停服后一起备份数据目录及 `--runs` 目录。

## 可选视觉辅助

默认关闭，普通 CAD 检查、人工确认与报价不依赖外部模型。启用前需为进程提供 `HVAC_VISION_ENABLED`、`HVAC_VISION_PROVIDER`、`HVAC_VISION_MODEL`、`HVAC_VISION_BASE_URL` 和 `HVAC_VISION_API_KEY` 环境变量配置服务，然后在账号设置启用。账号页面只管理启停偏好。仅设置自己的服务地址及凭据。

启用后，用户选中的图纸局部及相关上下文会发送到所配置的模型服务。发送前应检查预览；模型建议不自动改量或定价。账号启停偏好保存在本地运行数据中；服务凭据来自进程环境变量。不要将数据库或凭据文件提交到仓库。

源码启动不会自动读取 `.env`；需要由 shell 或启动工具注入环境变量。Compose 默认配置也不转发视觉服务凭据，使用时应自行配置容器环境。

## 常见问题

- **页面能打开，但生成失败**：确认 LibreOffice 可运行，中文字体可用，价格行与数量已确认。
- **DWG 无法读取**：确认 `dwg2dxf --version` 可用；也可先由 CAD 软件导出 DXF。
- **公式价格待重算**：用支持公式计算的软件重算并保存输入表，或在导入流程按提示处理。
- **重复生成演示时报目录非空**：指定新的 `--out` 目录，生成器不会覆盖现有项目。
