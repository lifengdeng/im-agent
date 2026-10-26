# im-agent

将你的专业能力变成可以协作的数字分身。

你已有的 AI 工程、工具和工作方法，往往需要由你亲自操作。im-agent 把这些能力连接到飞书：他人向分身提出问题，分身在授权范围内选择能力、调用你的本地工程，再把结果和依据回传，让你把精力留给判断与创作。

工程源码、业务接线、人格和运行数据可以独立保管，无需放进框架仓库。

## 工作方式

```text
飞书提问
  → transport：接收消息、核对身份
  → worker：在已注册能力中选择
  → 本机执行器：运行固定版本的工程程序
  → transport：回复结果并回读确认
```

- **接入自己的工程**：通过 Python JSON 桥接注册能力，由本地程序产生事实与结果。
- **独立实例**：初始化工具创建源码外的实例目录，分别保存配置、凭据和配对授权。
- **受限模型选择**：可用规则路由或 DeepSeek；模型只选择已注册能力，不能指定任意路径、Shell 或执行身份。
- **固定执行版本**：框架与工程使用 Git 快照，编辑开发目录不会改写正在运行的版本。
- **隔离与去重**：按用户和会话范围管理状态，持久保存受理、执行与交付记录；结果未知时保留现场供对账。

## 快速开始

需要 Python 3.11+ 和 Git。以下离线示例使用合成数据，不需要飞书凭据、模型 Key、SDK 或 Docker：

```sh
git clone https://github.com/lifengdeng/im-agent.git
cd im-agent
python3 -B scripts/init_instance.py init --instance-dir ../my-agent-instance --defaults
python3 -B scripts/init_instance.py demo --instance-dir ../my-agent-instance --runtime ../my-agent-demo --commit "$(git rev-parse HEAD)"
```

示例会创建独立实例，完成合成配对、提问、本机工程执行和结果回传。运行报告位于 `../my-agent-demo/onboarding-report.json`，成功时包含 `passed=true`、`reply_count=1` 和 `readback_verified=true`。

实例目录和运行目录必须是源码之外的新目录。命令从干净的 Git 检出导出固定版本，下载 ZIP 无法直接运行需要 Git SHA 的准备命令。Windows PowerShell 命令见[初始化引导](docs/初始化引导.md)。

## 接入自己的工程和飞书

1. 按[本地工程接入](docs/本地工程接入.md)实现 `im_agent_project.py` 桥接并注册能力。便捷入口支持一个独立 Git 工程、1–8 项能力和 Python 标准库依赖；复杂工程通过自己的 adapter 和依赖快照接入。
2. 使用初始化工具配置自己的飞书自建应用、模型和工程：

   ```sh
   python3 -B scripts/init_instance.py configure --instance-dir ../my-agent-instance
   ```

3. 按[配对与启动说明](docs/初始化引导.md#真实飞书私聊配对)完成应用权限配置、真人私聊配对及本机确认，再准备运行版本。transport 和 worker 在容器中运行，工程执行器在宿主运行。

默认示例使用规则路由；启用 DeepSeek 后，模型负责能力选择，答案中的业务事实仍由工程提供。完整多轮协作、主动监控和复杂业务权限需要实例扩展，见[能力边界](docs/数字分身能力边界.md)与[路线图](docs/实施路线图.md)。

## 文档

- [初始化、配置与配对](docs/初始化引导.md)
- [接入自己的本地工程](docs/本地工程接入.md)
- [数字分身示例与容器启动](docs/数字分身示例.md)
- [能力边界](docs/数字分身能力边界.md)
- [实例接入清单](docs/实例接入清单.md)
- [Windows 环境准备](docs/Windows环境准备.md)
- [架构决策](docs/架构决策.md)
- [开发规范](DEVELOPMENT.md)与[源码检查](docs/治理检查.md)
- [维护与发布](docs/维护与发布.md)、[部署边界](DEPLOYMENT.md)与[离线常驻部署](docs/PC常驻离线部署.md)
- [第三方组件](docs/第三方组件.md)

## 参与开发

在仓库根安装本仓库的 hooks 并运行检查：

```sh
python3 -B scripts/bootstrap.py install-hooks
python3 -B scripts/bootstrap.py check
python3 -B -m unittest discover -s tests -v
```

Windows 使用明确的 Python 3.11+ 解释器，例如 `py -3.11`。hooks 只配置当前仓库；业务实例的部署由各自维护流程管理。

## 许可

本项目暂未添加项目许可证，源码供查看和 GitHub fork。第三方组件适用各自许可证，详见[第三方组件](docs/第三方组件.md)。
