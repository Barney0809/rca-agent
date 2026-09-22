# AGENTS.md —— 进入本仓库前必读

> 本文件是**硬约束**，不是建议。任何代理（人或 AI）在本仓库工作前必须遵守。
> 最后更新：2026-09-22

---

## 🚫 第 0 条：绝对禁止删除任何东西

**这是最高优先级规则。违反它造成的损失已经发生过一次，且不可恢复。**

### 明确禁止的操作（任何路径、任何理由、包括临时目录）

| 类别 | 禁止的命令 |
|---|---|
| 删文件/目录 | `Remove-Item`、`del`、`erase`、`rd`、`rmdir`、`rm`、`shutil.rmtree`、`os.remove`、`Path.unlink` |
| 清空内容 | `Clear-Content`、`>` 覆盖已有文件、`truncate`、`DROP`、`TRUNCATE`、`DELETE FROM` |
| 清缓存/镜像 | `npm/pip/uv/pnpm cache clean`、`docker prune/rm/rmi/system prune`、`robocopy /MIR` |
| 动 git 历史或未提交内容 | `git clean`、`git reset --hard`、`git checkout -- .`、`git restore`、`git push --force`、`git branch -D`、`git stash drop/clear` |
| 移动或覆盖到已存在位置 | `Move-Item` 到已存在目标、`mv` 覆盖 |

### 允许与需要请示的界限

| 操作 | 规定 |
|---|---|
| **新建**文件/目录 | ✅ 随时可做 |
| **修改**已被 git 跟踪的文件 | ⚠️ 可做（可回滚），但必须说明改了什么 |
| **修改**未被 git 跟踪的已有文件 | 🛑 **先请示**（内容无法回滚） |
| **删除 / 移动 / 清空**任何东西 | 🛑 **绝对禁止。只把命令写出来交给用户执行** |

### 项目内的体现

- 本项目**刻意不提供** `make clean` / `distclean` 目标
- 策略执行点（`src/rca/policy/`）**不存在硬删除**：删除 = 移入隔离区 + TTL，可还原
- 见 `docs/harness-log.md` #1

---

## ⚠️ 第 1 条：编码纪律

### 可执行脚本必须纯 ASCII

`.ps1` / `.bat` / `.cmd` / `Makefile` 中**不得出现任何非 ASCII 字符**。中文只允许出现在 `.md` 文档里。

**原因（真实事故，已复现两次）**：

> Windows PowerShell 5.1 会把 **UTF-8 无 BOM** 的脚本文件按系统 ANSI（简体中文为 GBK）读取。
> 任何非 ASCII 文本都会被 mangle 成乱码。
>
> - 第一次：它让**路径解析**错位 → 删除了一个真实目录（不可恢复）
> - 第二次：它让**语法解析**错位 → 脚本直接崩溃

同一个根因，两种表现。见 `docs/harness-log.md` #1 与 #2。

### 其他编码要求

- **中文路径下不要用 PowerShell 读写文本文件**，改用 `read` / `write` / `edit` 工具
- 路径处理**全程按 UTF-8**；**禁止把路径交给 shell 做二次解析**（这是 #1 的直接成因）

---

## 📖 第 2 条：用户背景与沟通要求

**用户是 Python 新手。**（Java 背景很深：JVM / 连接池 / 多线程 / 线上排查。）

因此：

- **解释要详细**。不要假设 Python 语法、生态或惯用法已知
- 首次出现的 Python 概念要展开说明，必要时**对比 Java 的对应物**（这是用户最熟的语言）
- 命令要给出**完整可复制**的形式，并说明每条命令在做什么
- 报错要解释**为什么**会这样，而不只是"改这里就行"
- 类比优先：`asyncio` ↔ Java 线程池、`pydantic` ↔ Java Bean Validation、`uv` ↔ Maven

**如果用户说"太简略了"，就继续展开，不要压缩。**

---

## 🎯 第 3 条：项目目标（防止跑偏）

**核心命题**：

> **LLM 的"自觉"不可靠——所以要用确定性的工程机制，同时约束它的判断和它的行动。**

| 面 | 要解决的问题 | 机制 |
|---|---|---|
| **判断面** | 多视角信号冲突时避免误判 | 信息隔离 + 强制交叉举证 |
| **行动面** | 判断错了也不能造成不可逆损失 | 路径/动词校验 + 不可逆操作默认拒绝 |

**明确不做**（见 `docs/01-需求分析.md` §8）：
- ❌ 不承诺"让 AI 不再犯错"（无法交付；任何守卫都可能被绕过）
- ❌ 不作为"受害者叙事"（事故是需求来源之一，不是项目主题）
- ❌ 不引入任何第二语言（Go 已明确砍掉，全 Python）
- ❌ 不引向量库、K8s、前端大屏

---

## 🔧 第 4 条：技术事实（已实测核实，勿凭印象改动）

### 模型

| 事实 | 值 |
|---|---|
| 合法模型名 | **只有** `deepseek-flash` 和 `deepseek-v4-pro` |
| 旧名 | `deepseek-chat` / `deepseek-reasoner` **已于 2026-07-24 停服，不是别名** |
| BASE URL | `https://api.deepseek.com`（OpenAI 兼容） |

### ⚡ 三个已知坑

1. **`thinking` 默认是开启的** —— 不显式关闭会持续产生推理 token 费用，成本曲线失真
2. **带 `tools` 的多轮对话必须回传 `reasoning_content`，否则 400** —— 对多 Agent 反复调工具的场景极其致命
3. **缓存字段**是 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`

### 版本与 API

| 组件 | 版本 | 注意 |
|---|---|---|
| Python | 3.14.4 | 全部依赖有 cp314 wheel，**零本地编译**（已验证） |
| langgraph | 1.2.12 | `Send` 必须从 **`langgraph.types`** 导入（`langgraph.constants` 已废弃并会告警）；`create_react_agent` 已废弃 |
| mcp | 2.2.0 | **v2 重写了客户端 API**：只剩一个 `Client`；`FastMCP`→`MCPServer`；依赖换成 `httpx2` |
| langchain-openai | 1.6.3 | 走 OpenAI 兼容协议接 DeepSeek，不需要 DeepSeek 专用 SDK |
| 包管理 | uv 0.12.17 | 用 `python -m uv`（`uv` 命令需重开终端） |

### 环境现状

- 本机**没有 `pwsh`**，只有 Windows PowerShell 5.1 → 脚本统一用 `powershell -NoProfile -ExecutionPolicy Bypass -File`
- 本机**没有 `make`** → Windows 上直接用 `powershell -File scripts/dev.ps1`
- 自检：`powershell -NoProfile -File scripts/dev.ps1`

---

## 📚 第 5 条：文档地图

| 文件 | 内容 |
|---|---|
| `docs/01-需求分析.md` | 为什么做、必须满足什么、什么叫做完（含 ADR、验收标准 AC-1~AC-14） |
| `docs/02-技术方案与排期.md` | 技术选型、架构、15 天逐日排期、牺牲顺序 |
| `docs/03-事件流协议.md` | **D0 冻结**的接口契约（CLI 与前端共用） |
| `docs/harness-log.md` | 错误封堵清单——每加一行，系统就少一类失败 |
| `docs/adr/` | 架构决策记录 |

**冲突时以 `docs/01-需求分析.md` 为准。**

---

## ✅ 第 6 条：完成判定的标准

一条错误**只有在回归用例能变红之后**，才算封堵。
一个功能**只有在验收标准可被第三方复现之后**，才算完成。

第三方复现的门槛：**陌生人克隆仓库 → 一条命令起全栈 → 复现全部数字**（含无 API Key 的离线回放）。
