# PC 常驻离线候选

**本页所有 activate、stop、rollback 命令只操作离线 resident。生产实例必须使用其实例维护手册；通用约束见 [维护与发布规范](维护与发布.md)。**

`resident_pc.py` 只接受已固定、干净且通过治理检查的两个 Git 提交。私有实例声明可信适配器工厂，镜像在准备阶段构建；激活后使用镜像 ID 和导出的固定源码。后台不拉取镜像、更新源码或安装工程依赖。

实例的 `instance-release.json` 通过 `native_adapter_factory` 声明宿主适配器工厂，通过 `offline_resident_verifier` 声明离线验收入口；二者只能来自固定私有 Git 导出中的相对 Python 文件。工程名称、业务样例与查询结果断言放在私有实例，公共部署器不固化它们。使用者接入工程时实现相同工厂/验收协议即可。

## 准备并启动

在开发环境修改、测试、提交并推送。实例的 `instance-release.json.framework_commit` 必须指向已提交框架版本。PC 只通过 Git 更新，保留所有现场改动。

```powershell
py -3.11 -B scripts/resident_pc.py prepare --runtime-root <独立运行根> --private-source <私有实例检出> --native-project <既有工程> --commit <框架完整SHA> --instance-commit <实例完整SHA>
py -3.11 -B scripts/resident_pc.py check --runtime-root <独立运行根> --release <prepare返回ID>
py -3.11 -B scripts/resident_pc.py activate --runtime-root <独立运行根> --release <prepare返回ID>
py -3.11 -B scripts/resident_pc.py startup --runtime-root <独立运行根>
py -3.11 -B scripts/resident_pc.py verify --runtime-root <独立运行根>
py -3.11 -B scripts/resident_pc.py status --runtime-root <独立运行根>
```

`prepare` 检查快照、专用 CLI、基础镜像摘要与依赖哈希，再运行核心、私有基线和业务适配回归。`activate` 只替换本部署器带归属标签的容器与执行器，保留数据库。`check` 校验发布目录全部文件。`stop` 停止候选并保留账本及人工停止标记。`rollback` 仅接受此前保存的 schema 3 候选，不回退工程快照或删除历史合同；没有兼容旧候选时拒绝回滚。

## 常驻与恢复

- 两个容器以非 root、只读根、去除 capabilities、限制内存/进程和日志轮转运行。worker 只发布宿主 loopback 端口。内部工程结果不直接进入 IM 投递。
- 原生 supervisor 持有 OS 生命周期锁。后台每 15 秒监听版本，前台执行任务；重型快照准备不占接单线程。
- watcher 先装载历史处理器，再向 worker 持久发布新合同。注册回执未知可对账重试；执行未知保持 unknown，禁止重跑。旧任务固定旧合同和依赖快照。
- Docker 未就绪时退避，恢复后检查并启动自身容器；不会从 SSH 重启 Docker Desktop。已完成投递幂等；未知投递不自动重发。
- Windows 使用当前用户的 `ImAgent-Offline-Resident` 登录计划任务，有限权限，不保存密码，不使用 SYSTEM。`IgnoreNew` 与 OS 锁防止重复实例；失败后由计划任务重试。
- 计划任务必须使用 `pythonw.exe`，后台 Docker、Git、PowerShell、原生 Python 和 Codex 子进程统一使用 `CREATE_NO_WINDOW` 与隐藏窗口启动信息；仅重定向输出不能阻止 Windows 分配控制台。手动 CLI 登录仍保留交互。发布须在用户桌面会话观察启动和多个定时检查周期，确认没有新增终端窗口；SSH 会话的无输出不能代替桌面验证。

**用户级 Docker Desktop 支持登录后常驻与锁屏继续运行，不保证注销后或重启但无人登录时运行。电脑睡眠期间也不会接单。** 无人登录的机器服务需要另外部署相应引擎和账户。验收不会重启、注销、锁屏或更改电源策略来打断用户。

## 离线边界

此入口只接收合成 actor 的文件 inbox。私有 worker 可用本地 HTTP fixture 验证实例声明的离线行为；宿主查询能力与结果标注由实例合同固定。离线 profile 没有生产开关，其结果不代表真实模型、工程业务调用或 IM 接入已验收。独立数字分身示例的验证范围见[示例说明](数字分身示例.md)。

令牌、账本和报告位于源码外的 `resident/`，不进入 Git。生产机器人、凭据和真实历史数据不参与离线验证。
