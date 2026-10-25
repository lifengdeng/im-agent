# Windows 本机环境准备

本工具准备 Docker Linux 引擎和独立 Codex CLI。环境准备不包含应用源码、人格、记忆、工程适配器或飞书连接；应用接入另按[本地工程接入](本地工程接入.md)进行。

## Codex 独立安装

使用 Python 3.11+ 标准库。锁文件 `config/windows-toolchain.json` 固定官方完整 Windows x64 包的版本、下载大小和 SHA-256，当前固定为 0.162.1；采用其他版本前分别核对兼容性。安装优先使用官方 npm Windows 平台分发包，其摘要与同版本 GitHub release 资产核对；完整保留 vendor 下的原生运行树、依赖与许可证，直接执行原生 CLI，不需要全局 npm 安装。

在已经通过 Git 更新、无本地修改的源码目录运行，`$runtimeRoot` 指向自行选择的源码根以外的专用目录：

```powershell
py -3.11 -B scripts/windows_environment.py prepare-codex --runtime-root $runtimeRoot
py -3.11 -B scripts/windows_environment.py status --runtime-root $runtimeRoot
```

`status` 同时探测本机 Docker Desktop Linux 管道，只报告服务端版本与平台，不跟随环境变量中的远端 Docker 地址。`engine_ready` 与 `agent_deployed` 分开记录；引擎就绪不表示应用已部署。

目录布局：

```text
<runtimeRoot>/
  instance.json                  专用环境标记，无凭据
  bin/codex-agent.cmd             私有快捷启动入口
  tools/codex-<固定版本>/          完整官方包及文件校验回执
  downloads/                     已校验安装包
  codex-home/config.toml          专用配置
  codex-home/auth.json            登录后产生，不读取、不提交
  codex-home/...                  会话、日志、缓存等独立状态
  workspace/                     登录和本机检查的默认工作目录
```

运行根关闭继承权限，只授予当前 Windows 身份和 SYSTEM 完全控制。安装器拒绝源码内目录、个人 `.codex`、符号链接/重解析路径、无归属的现存目录、校验失败的安装包和覆盖已有不同内容。升级选择新的固定版本目录，不原地改写已有包。

快捷入口只给子进程设置 `CODEX_HOME`，清除继承的模型认证/地址环境覆盖，默认进入专用工作目录；不改全局 PATH、不设置用户级 `CODEX_HOME`，不复制桌面客户端认证。配置固定 `cli_auth_credentials_store = "file"`，使登录落在专用目录，不复用个人系统凭据存储。默认 workspace-write、on-request，未提前放宽业务权限。

需要登录时，在 PC 自己的 PowerShell 中运行：

```powershell
& "$runtimeRoot\bin\codex-agent.cmd" login
& "$runtimeRoot\bin\codex-agent.cmd" login status
```

登录由本人完成；无需在聊天中提供凭据、验证码或认证文件。准备完成不代表已登录、Windows 沙箱已验收或已做模型调用。独立目录用于区分配置和登录态；当前仍由同一 Windows 用户运行，完整系统权限隔离属于后续本机执行器设计。

## Docker 与 WSL

优先复用已有 Docker Desktop，不重置设置，不清理或重建用户已有容器。先核对 Docker Desktop 安装路径、Windows 版本、WSL、VirtualMachinePlatform 和硬件虚拟化，再启动已有引擎：

```powershell
docker desktop start --detach
docker version
docker info --format '{{json .OSType}}'
```

若 WSL 仅有旧系统入口且缺内核，可使用微软官方 `wsl --update --web-download`。如果该路径不可用，可由管理员执行本仓库的固定 MSI 安装入口；它校验锁文件中的 SHA-256 及微软有效签名，以 `/qn /norestart` 安装 WSL 并保留本机日志，不安装 Ubuntu、不重置任何发行版：

```powershell
py -3.11 -B scripts/windows_environment.py prepare-wsl --runtime-root $runtimeRoot
```

缺少可选组件时仅启用所需组件并保留待重启状态，不自动重启正在使用的 PC。BIOS/UEFI 的硬件虚拟化必须在机器端开启。

若现有 Docker Desktop 安装损坏、缺少安装注册信息，可通过固定版本安装包修复。`prepare-docker` 校验官方 SHA-256 和 Docker 有效签名，按用户安装 WSL 后端，不执行卸载、数据重置、prune 或自动接受协议。首次使用如出现协议界面，由使用者在 PC 本人完成。修复前必须确认没有需保留运行的容器；该入口不能当作常规升级的无条件重启入口。

```powershell
py -3.11 -B scripts/windows_environment.py prepare-docker --runtime-root $runtimeRoot
```

首次安装建议在 PC 桌面会话的 PowerShell 中执行。若 SSH 后台安装报 `AutoStartAction / UnauthorizedAccessException`，先保留安装日志，再从同一用户的桌面会话执行相同版本化入口，由本人处理系统/安全软件提示。不要改写受保护注册表权限、关闭安全防护或手工伪造安装注册信息。桌面安装成功后，日常业务发布不应重复运行此安装器。

Docker Desktop 的长期运行进程也应从桌面会话或已批准的启动机制启动。Windows SSH 会话退出时可能结束其启动的子进程；远端 `desktop restart` 返回接受请求，不等于断开 SSH 后引擎仍存活。必须在新的 SSH 连接中回读本机管道，再做容器验证。后续应用发布只消费已就绪的引擎，不把重启 Docker Desktop 当作发布步骤。

Windows SSH 登录还可能无法访问系统 Credential Manager。公共镜像自检使用独立的 Docker CLI 配置，显式指定本机引擎；匿名配置可使用 `{"auths":{"https://index.docker.io/v1/":{}}}`。空 `auths` 会使 Docker CLI 自动探测默认凭据存储，继而可能报 `A specified logon session does not exist`。不要为自检复制桌面 Docker 认证或改动全局 credential helper；私有镜像的部署凭据在应用部署阶段独立配置。

验证须记录 Linux Server 实际响应；临时测试容器使用固定镜像摘要、无端口发布和宿主目录挂载，只删除本次自己创建的容器。验证不运行实例 Compose、不建立生产连接、不配置机器人开机启动。引擎无法启动时应明确记录阻塞，不能只凭 Docker CLI 安装成功宣称引擎就绪。

## 官方依据

- [Windows CLI 安装及完整包要求](https://learn.chatgpt.com/docs/enterprise/windows-deployment)
- [Codex 配置和状态目录](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)
- [认证存储](https://learn.chatgpt.com/docs/auth#credential-storage)
- [Docker Desktop CLI](https://docs.docker.com/desktop/features/desktop-cli/)
- [Docker Windows 安装和依赖](https://docs.docker.com/desktop/setup/install/windows-install/)
- [微软 WSL 命令](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)
