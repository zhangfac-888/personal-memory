# Personal Memory 安装说明（给 AI 助手）

这份文件写给**替用户安装的 AI 助手**，不是给用户自己读的快速上手。目标：用户把仓库链接交给你，你完成检查、安装、校验和汇报；用户只需要做三件机器上必须由人做的事。

## 0. 你能做和不能做的事

你可以：
- 读取本文件、检查环境、运行 `install.ps1` / `verify.ps1` / `uninstall.ps1`。
- 在本机安装、启动、停止这个服务，读取它自己写出的日志和 JSON 结果。
- 在用户明确同意后，用 `-Connect` 把**指定的**一个客户端接上。

你不能：
- 让用户把 DeepSeek API key、管理密钥（`admin-key.txt` 的内容）粘贴到聊天里。
- 读取或转述 `Data\admin-key.txt` 的内容；需要登录网页时，让用户自己打开那个文件。
- 替用户点客户端里的“信任”提示，或替用户决定要不要开启自动采集。
- 把“安装成功”“客户端配置成功”说成“模型已经在用这些记忆”。这是两件事。

必须由用户亲自做的三件事：
1. 确认安装（你可以在用户已经明确要求安装后，用 `-Yes` 代为确认）。
2. 输入自己的 DeepSeek key（在隐藏输入里打，见第 7 节）。
3. 在 AI 客户端里确认它自己的 MCP/工具信任提示。

## 1. 环境前提

- Windows 11 x64。ARM64 或 32 位系统会被脚本直接拒绝。
- Windows PowerShell 5.1（系统自带）即可。
- 需要联网：脚本会从官方地址下载固定版本的 uv 和 CPython 3.11.11，再按 `uv.lock` 安装依赖。不需要预先安装 Python、uv 或 Node。
- 不需要管理员权限。安装目录在用户自己的 `%LOCALAPPDATA%` 下，登录自启任务也只属于当前用户。
- 大约需要 1 GB 磁盘空间（运行时 + 依赖 + 首次数据库）。

## 2. 获取安装包

两种来源都可以，但**必须拿到完整仓库内容**，因为 `install.ps1` 会按 `package-manifest.json` 校验每一个文件：

```powershell
git clone https://github.com/zhangfac-888/personal-memory.git
```

或者让用户下载 Release 里的 ZIP 并**完整解压**（不要只解压一部分，也不要直接在 ZIP 预览里运行）。

先进入包含 `install.ps1` 和 `package-manifest.json` 的那个目录：

```powershell
Set-Location <解压或 clone 后的目录>
```

## 3. 预检（不写任何东西）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -CheckOnly -Json
```

看到 `PM_RESULT {"status":"package_verified",...}` 且退出码为 0，说明包完整、哈希一致、系统是 Windows x64。**预检不安装、不下载、不改配置。**

如果这里失败：先解决包不完整或系统不支持，不要继续。

## 4. 安装（无人值守）

在用户明确同意后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes
```

- `-Yes` 表示无人值守：不再询问“是否安装”、跳过 key 步骤、跳过复制管理密钥到剪贴板。
- 安装位置：`%LOCALAPPDATA%\PersonalMemoryShare\App`（程序）和 `...\Data`（数据库、密钥、日志）。
- 会创建一个当前用户的“登录时启动”计划任务；如果系统策略不允许，脚本会退回“只启动本次会话”，并提示以后用 `Start.cmd`。
- 不会动已有的服务：端口被别的进程占用时直接报错退出，不会强杀。

成功时最后一行是：

```
PM_RESULT {"status":"installed","app":"...","data":"...","port":8765,"snapshot":"...","service":"running","startup_task":"configured","connected":{},"warnings":[]}
```

退出码：`0` 成功；`1` 失败（看 `error` 字段）；`2` 安装成功但有客户端没接上（看 `connected`）。

失败时保留原始错误，不要反复重试超过两次；先按第 10 节判断原因。

## 5. 校验（只读）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\verify.ps1 -Json
```

最后一行 `PM_VERIFY {...}`，退出码 `0` 表示所有必需项通过：包哈希、安装身份、数据目录标记、服务健康、数据库完整性、schema 版本。

`PM_VERIFY.checks` 里 `required=true` 的项必须全部为 `true`；`required=false` 的是信息项，例如：
- `deepseek_key`：是否已配置加密 key。没有也**不算安装失败**，只是关闭 AI 提炼。
- `semantic_model`：本机 Ollama 是否有 `bge-m3:latest`。没有就只做关键词检索。
- `clients`：哪些客户端自动化配置指向这个安装。

校验只读，不改文件、不启动服务、不读密钥内容。

## 6. 可选：连接 AI 客户端

默认不连接。先问用户要接哪个客户端，再执行**一个**：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -Connect claude
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -Connect codex
```

可用的值：`claude`、`codex`、`workbuddy`、`opencode`，也可以逗号分隔多个。脚本在服务已经装好时也能单独补连（会走“已安装并运行”分支）。

执行前必须向用户说明差异：

| 客户端 | 会改什么 | 影响 |
| --- | --- | --- |
| `claude` | Claude Code 用户级 hooks（4 个事件）+ 只读 MCP | 每轮自动采集对话片段到本机数据库；任务保护钩子可能在缺少任务合约时**拒绝写操作**；配置的提炼会把选中文本发给 DeepSeek |
| `codex` / `workbuddy` / `opencode` | 只读 MCP 条目 | 只能主动检索，不采集、不写入 Active |

如果这台机器上已经有**另一个** Personal Memory 安装连着同一个客户端，脚本会输出 `skipped_other_installation` 并拒绝改动；必须先卸载那个安装。不要手工改它的配置或环境变量来绕过。

`Connect-Agents.cmd` 是同一件事的交互入口，但只认默认安装位置（`%LOCALAPPDATA%\PersonalMemoryShare`）；用 `-Root` 装的人请用上面的 `-Connect`。它同样会先检查客户端是否已连别的安装，若已连则输出 `already connected to another Personal Memory installation`、退出码 `2`，不改任何客户端设置。

连接后：
- 必须让用户重启或重新加载对应客户端，并确认客户端自己的信任/工具提示。
- “配置成功”不等于“客户端原生聊天已实际用上记忆”。在用户真实提问并看到检索/采集结果前，只能报“已配置，待原生验证”。
- 客户端连接失败不会影响安装本身；退出码为 `2`，`connected` 里会写明哪个失败。

## 7. 可选：配置 DeepSeek key（只能由人输入）

没有 key 时：网页、手动记录、关键词检索仍可用；自动/手动 AI 提炼关闭，事件会保留并明确报错。

让用户自己双击 `Configure-Key.cmd`，或者在当前目录直接运行：

```powershell
.\Configure-Key.cmd
```

实际执行的是隐藏输入（`getpass`），key 用 Windows DPAPI 加密保存到 `Data\secrets\deepseek-key.dpapi`，文件夹 ACL 会收紧。**你不要读这个文件，也不要让用户把 key 发给你。** 配置完成不代表已经发起过 API 请求，也不产生费用；只有实际提炼时才发送选中文本。

## 8. 可选：语义检索

安装只保证关键词/全文检索。语义检索需要用户另外：

1. 安装 Ollama（`https://ollama.com`）。
2. `ollama pull bge-m3`。
3. 重新运行 `verify.ps1`，确认 `semantic_model` 为 `PASS`。

后端按固定模型摘要校验；如果拉到的 BGE-M3 版本和程序内置摘要不一致，服务会明确报“向量模型版本与索引不一致”，此时语义检索不可用，不要把它说成已就绪。

## 9. 向用户汇报什么

按这四项分开说，不要合并：

1. **安装与校验**：`PM_RESULT` / `PM_VERIFY` 的结论、退出码、安装路径和端口。
2. **能力状态**：只有关键词检索 / 已配 key；语义检索是否就绪（Ollama+BGE-M3）。
3. **客户端**：连了哪个、改了什么、是否需要重启、哪些还没在原生聊天里验证。
4. **未做/未验证**：例如“没开自动采集”“没在另一台机器验证”“没观察模型是否采用了注入内容”。

管理密钥在 `%LOCALAPPDATA%\PersonalMemoryShare\Data\admin-key.txt`，让用户自己打开并只粘贴到本地登录页。

## 10. 常见失败

| 现象 | 处理 |
| --- | --- |
| `Missing package file` / `File checksum mismatch` | 包不完整或被改过。重新完整解压或重新 clone，不要跳过校验。 |
| `This preview supports Windows x64 only` | 系统不支持，停止安装。 |
| `Port 8765 is already in use` | 有别的服务占用。脚本不会停它。让用户决定换端口（`-Port`）还是先处理那个服务。 |
| 下载失败 / `uv checksum mismatch` | 网络或镜像问题。重试一次；校验不通过时脚本不会执行下载的二进制。 |
| `Service not ready` | 看 `Data\server.stderr.log` 和 `server.stdout.log`。常见原因是依赖没装全或端口刚刚被抢。 |
| `Different existing installation` | 这个目录已经装过别的 snapshot。让用户先运行 `uninstall.ps1`，再做新安装；不要手工删身份文件。 |
| 网页登录失败 | 让用户重新打开 `admin-key.txt`；不要把 key 发到聊天里。 |
| 语义检索不工作 | 检查 Ollama 是否运行、`bge-m3:latest` 是否存在、摘要是否匹配。 |

失败后保留现场：不要删除 `Data`、日志、`share-installation.json`，也不要反复重装覆盖证据。

## 11. 卸载

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 -Yes
```

- 只停止命令行确实指向这个安装的进程；不认识的监听进程不动。
- 只删除参数完全匹配的计划任务。
- 默认先反注册指向这个安装的客户端，避免 Claude hooks 还指向已删除的 python.exe。
- **数据默认保留**；要删库必须显式 `-RemoveData`，并且会再问一次。
- 解压目录/仓库目录永远不动。

没有 `share-installation.json` 的目录会被拒绝（退出码 `3`），防止误删别人的安装。

## 12. 边界（不要过度承诺）

- 这是 Windows x64 预览包。本机安装成功不等于在所有环境一键成功。
- 安装不等于客户端原生闭环：MCP 能被发现、hooks 能触发、记忆能检索，都需要在用户真实使用中分别观察。
- 自动采集默认关闭；Claude 连接是唯一会启用采集的选项，且需要用户明确同意。
- 代码回归数、历史基准成绩不是本安装包的质量证明，也不代表回答准确率。
- 不要把这套说明改写成“永不出本机”：开启 DeepSeek 提炼后，选中文本会发到 DeepSeek。
