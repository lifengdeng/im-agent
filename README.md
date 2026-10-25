# im-agent

将你的专业能力变成可以协作的数字分身。

接入已有的 AI 工程、工具和工作方法，让同事通过飞书提出问题，由分身在授权范围内理解请求、调用本地能力并回传有依据的结果，把日常执行精力留给更需要本人判断和创作的工作。

## 公开与许可状态

本项目暂不采用开源许可证。源码公开的范围限于审定文件的查看和 GitHub fork；这不是对商业使用、再分发或其他使用方式的额外授权。第三方组件继续适用各自许可证。私有开发历史、个人邮箱、业务工程及实例接线保持私有，公开操作单独执行。具体边界见[公开准备审查](docs/公开前审查.md)。

## 先体验完整链路

需要 Python 3.11+、Git 和一个干净的 Git 检出目录。在仓库根运行以下两步即可体验；离线体验不需要飞书凭据、模型 Key、SDK 或 Docker：

```sh
python3 -B scripts/init_instance.py init --instance-dir ../my-agent-instance --defaults
python3 -B scripts/init_instance.py demo --instance-dir ../my-agent-instance --runtime ../my-agent-demo --commit "$(git rev-parse HEAD)"
```

Windows PowerShell 的明确解释器命令、交互配置及配对步骤见[初始化引导](docs/初始化引导.md)。GitHub 下载 ZIP 或源码压缩包不含 Git 元数据，可供查看和核验文件；要运行需要固定 SHA 的 `demo` / `prepare-live`，请使用干净的 Git clone。实例目录和运行目录都必须是新目录，并位于框架源码之外。

[初始化引导](docs/初始化引导.md)在源码外创建自己的实例目录；`demo` 验证合成配对、提问、宿主工程执行、单条回复和回读。后续可分步填写飞书与模型凭据、选择私有本地工程、完成真人私聊配对；秘密与运行数据留在独立实例中。

[数字分身示例](docs/数字分身示例.md)提供独立实例模板和合成工程：同事询问最近一次发布，宿主执行工程程序，结果经飞书接口形状的 HTTP 服务回复并回读核验。默认不联网、不调用模型；可显式配置独立测试应用使用真实飞书长连接，也可启用受限模型能力选择。

要接入自己的工程，从[本地工程接入](docs/本地工程接入.md)开始：复制独立工程模板，注册能力，以完整 Git SHA 导出宿主快照，再连接自己的飞书应用和模型。工程和业务接线可以保持私有，无需修改框架源码。

| 验证范围 | 状态 |
|---|---|
| 合成飞书事件 → 持久受理 → 本地程序 → 回复与回读 | 提供可重复运行的离线 smoke 和行为测试 |
| 真实飞书长连接、官方回复 API、回读 | 已通过独立应用的一次真实提问、Linux 宿主合成工程执行和单条回复回读；规则路由、无模型调用 |
| 真实模型选择能力 → 本地程序 → 飞书回复与回读 | 已通过 `deepseek-flash` 的一次真实调用；能力、模型回执和执行结果均已核对 |
| 独立本地工程接入 | 支持单工程、1–8 项能力及标准库 Python JSON 桥接；独立 Git 快照、模型选择与原生执行有离线测试 |
| 初始化实例 → 真人私聊配对 → 模型 → 独立工程 → 回传 | 2026-10-25 已在 Linux 隔离应用完成真实验收：生成的通用模板工程、1 次模型调用、1 次宿主执行、1 条回复及回读；见[验收范围](docs/初始化引导.md#已验证范围) |
| 复杂依赖工程、完整多轮协作、主动监控审阅 | 通过私有 adapter 和注册合同逐步扩展，须分别验收 |

通用核心、实例专属代码和运行数据的归属见[数字分身能力边界](docs/数字分身能力边界.md)。实例可以保持私有，公开示例独立运行。

## 当前状态

公共框架提供 transport / worker 容器、宿主执行协议、隔离账本与记忆、独立工程快照、Codex stdio 客户端和后台版本跟随。默认部署入口仍为离线候选；生产接线、业务认证、真实人格和实例验收属于私有实例，不能直接套用离线 activate/rollback。

后续开发、固定版本发布、状态保全和回滚遵循 [维护与发布规范](docs/维护与发布.md)。离线入口的准确用法见 [PC 常驻离线部署](docs/PC常驻离线部署.md)，实例的生产操作须读其实例维护手册。

目标架构为：

```text
同事提问 / 飞书
  → transport 容器：事件与身份、交付与回执
  → worker 容器：模型、人格、记忆、任务分派
  ← PC 本机执行器：主动领任务、受限 Codex / 工程适配器
                         └─ 已注册工程的独立发行快照
```

实例负责具体业务接线、人格和工程 adapter。公共核心不携带公司接口、个人登录、真实记忆或私有工程源码。

## 开发准备

Mac / Linux：

```sh
python3 -B scripts/bootstrap.py install-hooks
python3 -B scripts/bootstrap.py check
python3 -B -m unittest discover -s tests -v
python3 -B scripts/bootstrap.py doctor --json
```

Windows PowerShell：

```powershell
py -3.11 -B scripts/bootstrap.py install-hooks
py -3.11 -B scripts/bootstrap.py check
py -3.11 -B -m unittest discover -s tests -v
py -3.11 -B scripts/bootstrap.py doctor --json
```

以上 bootstrap 命令仅使用 Python 3.11+ 标准库，不安装依赖，不启动 Docker、机器人或本机服务。安装 hooks 只修改当前仓库的 Git 本地配置，不修改全局 Git 或默认 Python。业务运行时的 Python 版本会在平台验证后另行冻结。

## 文档

- [公开准备审查与已确认决定](docs/公开前审查.md)
- [自动创建实例与配对引导](docs/初始化引导.md)
- [接入自己的本地工程](docs/本地工程接入.md)
- [数字分身完整示例与真实验收范围](docs/数字分身示例.md)
- [维护与发布规范](docs/维护与发布.md)
- [开发规范](DEVELOPMENT.md)
- [部署规则与当前边界](DEPLOYMENT.md)
- [架构决策](docs/架构决策.md)
- [实例接入与验收清单](docs/实例接入清单.md)
- [治理检查的范围](docs/治理检查.md)
- [能力路线图](docs/实施路线图.md)
- [Windows Docker / 独立 Codex 环境准备](docs/Windows环境准备.md)

## 源码和运行数据

本仓库维护通用框架与开发治理；个人实例和业务 adapter 独立配置。`.local/` 只保存本机准备与审查材料，运行数据和凭据不提交。源码展示仅消费审定文件快照，保留私有开发库和仍被运行实例引用的版本；不通过切换私有开发库的可见性暴露旧记录。
