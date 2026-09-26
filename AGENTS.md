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

### 运行脚本必须显式指定解释器

```powershell
# 对
.\.venv\Scripts\python.exe scripts\reduce_run.py
uv run python scripts\reduce_run.py

# 错 —— 它会用系统 Python，看不到项目依赖
python scripts\reduce_run.py
```

**为什么这条要单列**（见 `docs/harness-log.md` P1）：
用系统 Python 跑脚本**不会报错**，而是给出**一个错误的事实** ——
例如对明明装好的包报 `ModuleNotFoundError`。
那看起来像"环境坏了"，实际是"命令写错了"。

`scripts/dev.ps1` 会交叉检查裸 `python` 指向哪里，不一致就提醒。

### 提交信息用 `-F <文件>`，不用 `-m <字符串>`

```powershell
# 对 —— 先把信息写进文件，再整体传入
git commit -q -F runs\_commitmsg.txt

# 错 —— 信息里若有 " 或换行，PowerShell 会把它截断成多个参数
git commit -m $longMessage
```

**为什么**（见 `docs/harness-log.md` P3）：PowerShell 5.1 把含双引号的字符串
传给**原生命令**时会重新拼命令行，内嵌的 `"` 会截断字符串，
后半段变成独立参数传给 git。

⚠️ 这个失败**容易被漏掉**：`git add` 是成功的，只有 `commit` 失败，
而错误信息埋在输出里。所以**提交后要核对 `git log`**。

同一原则适用于任何传给原生命令的**长文本** —— 有引号、换行或非 ASCII 时一律走文件。

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

### ⚡ 已实测的坑（D0 spike 结论，2026-09-24，非二手资料）

**坑 1：`thinking` 默认开启，而且真的在计费。**

实测：让模型回答"收到"两个字，`usage.completion_tokens_details.reasoning_tokens = 13`。
在带工具的两轮对话里，推理 token 占输出 token 的 **15%–26%**。

关闭方式**只有一种有效**：

```python
extra_body={"thinking": {"type": "disabled"}}
```

| 候选写法 | 实测结果 |
|---|---|
| `thinking={"type":"disabled"}` | ✅ 有效，`reasoning_content` 消失 |
| `enable_thinking=False` | ⚠️ **请求成功但被静默忽略**——以为关了，其实一直在付钱 |
| `thinking=False` | ❌ 422 |

> `enable_thinking=False` 是最危险的一个：**不报错、不告警**，成本对账时会莫名其妙对不上。

**坑 2：「多轮 tools 必须回传 `reasoning_content` 否则 400」—— 实测未复现，判为不成立。**

四种组合（第2轮 thinking 开关 × 是否回传）在 `deepseek-flash` + OpenAI 兼容接口 + 两轮工具调用下**全部成功**。

**仍建议保留该字段，但理由变了**：不是为了通过接口校验，而是**成本核算与调试需要看到推理内容**。

> 未覆盖：`deepseek-v4-pro`、3 轮以上、Anthropic 兼容端点（`/anthropic`）。若在这些场景遇到 400，优先怀疑这条。

**坑 3：缓存字段有两套，都要认。**

- DeepSeek：`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
- OpenAI 标准：`prompt_tokens_details.cached_tokens`

缓存是**自动**的、无需显式开启；实测在第 2 轮就出现命中（`cache_hit=128`）。

**坑 4：stdout 被重定向到管道/文件时，Python 会用本地编码 GBK，打印 emoji 直接崩溃。**

本机控制台代码页 936。`sys.stdout` 接到**真实控制台**时 Python 走 `WriteConsoleW`，
不受代码页限制；但接到**管道或文件**时（脚本被上层采集、输出重定向）会退回 `cp936`，
此时 `print("✅")` 抛 `UnicodeEncodeError`，**整段输出连同退出码一起丢掉**。

三种写法实测对比（2026-09-24，`.venv` 下真实管道捕获，
用 `scripts/probe_stdout_encoding.py` 复跑即可复现）：

| 兜底写法 | 中文 | emoji | 结论 |
|---|---|---|---|
| 完全不兜底 | GBK 字节 | **崩溃** | ❌ |
| `reconfigure(errors="replace")` | 仍是 GBK 字节 → 读成乱码 | `?` | ❌ 不崩但读不成 |
| `reconfigure(encoding="utf-8", errors="replace")` | 正常 | 正常 | ✅ |

**必须同时给 `encoding` 和 `errors`，缺一不可。** 只加 `errors="replace"`
是个很自然的误判（本项目作者第一版就是这么写的，还配了"不要用 utf-8，会乱码"的错误注释，
被上表直接推翻）。

子进程同理：用 Python 启动子进程并重定向它的输出时，要给它
`env["PYTHONIOENCODING"] = "utf-8"`，否则你声明的"UTF-8 日志"里其实是 GBK 字节。

> 同一个编码机制在本项目已造成 **4 次**不同后果（删目录 / 脚本崩溃 / 数据读错 / 输出丢失），
> 见 `docs/harness-log.md` #1 #2 #3 P4。封堵方式：`tests/test_offline.py` 的
> `test_regression_p4_*` 自动检查（**会变红**，已用变异测试证明）。

### 版本与 API

| 组件 | 版本 | 注意 |
|---|---|---|
| Python | 3.14.4 | 全部依赖有 cp314 wheel，**零本地编译**（已验证） |
| langgraph | 1.2.12 | `Send` 必须从 **`langgraph.types`** 导入（`langgraph.constants` 已废弃并会告警）；`create_react_agent` 已废弃 |
| mcp | 2.2.0 | **v2 重写了客户端 API**：只剩一个 `Client`；`FastMCP`→`MCPServer`；依赖换成 `httpx2` |
| `openai`（SDK 直连） | ≥3.17.0 | 走 OpenAI 兼容协议接 DeepSeek，**不需要 DeepSeek 专用 SDK**。（`langchain-openai` 1.6.3 是等价备选路径，但**代码里没有用它**；2026-09-27 已把它连 `langchain-core` / `structlog` 一起从依赖里移除 —— 声明了却没人 import，见 harness-log #68） |
| 包管理 | uv 0.12.17 | 用 `python -m uv`（`uv` 命令需重开终端） |

### 环境现状

- 本机**没有 `pwsh`**，只有 Windows PowerShell 5.1 → 脚本统一用 `powershell -NoProfile -ExecutionPolicy Bypass -File`
- 本机**没有 `make`** → Windows 上直接用 `powershell -File scripts/dev.ps1`
- 自检：`powershell -NoProfile -File scripts/dev.ps1`

---

## 📚 第 5 条：文档地图

| 文件 | 内容 |
|---|---|
| `docs/00-状态.md` | **进度与下一步的唯一权威**（新会话先读这一份 + 本文件） |
| `docs/01-需求分析.md` | 为什么做、必须满足什么、什么叫做完（含 ADR、验收标准 AC-1~AC-14） |
| `docs/02-技术方案与排期.md` | 技术选型、架构、15 天逐日排期、牺牲顺序 |
| `docs/03-事件流协议.md` | **D0 冻结**的接口契约（CLI 与前端共用） |
| `docs/04-故障目录.md` | 八种故障的定义、症状/根因位置、标准答案（**含"真值是否已验证"这一列，且由用例守住**） |
| `docs/05-单Agent基线结果.md` | D5 baseline 的完整结论（含两次数字作废的记录） |
| `docs/06-多Agent结果.md` | **多 Agent 到底有没有用**（D7 的对照结论 + **证据索引**：每个数字 → 存档 → 复现命令） |
| `docs/07-面试讲述稿.md` | 3 分钟 / 10 分钟讲述 + 40 分钟追问准备（**对外讲述用**） |
| `docs/08-项目总览.md` | **3 分钟看懂这个项目做了什么** + 导航表（给第一次打开仓库的人 / 新代理） |
| `docs/架构图.svg` | 一页架构图（**绿实线 = 已实现，红虚线 = 未接线**，投屏用） |
| `docs/09-演示视频分镜.md` | 演示视频的分镜/命令/台词（**录制前照这个准备**） |
| `docs/10-护栏设计.md` | **护栏**（运行时监督/拦截）的设计与验收形状：三层结构、标准事件接口、M1 清单、**刻意不做**的边界 |
| `docs/harness-log.md` | 错误封堵清单——每加一行，系统就少一类失败 |
| `docs/adr/` | 架构决策记录 |
| `demo/rca-demo.html` | **公开实例**：一个自包含页面，由存档生成、可逐字节重放（用 `scripts/make_demo.py`） |

**冲突时以 `docs/01-需求分析.md` 为准。**
**进度、计数、完成状态以 `docs/00-状态.md` 为准，其他文档禁止复制。**

### 工具

| 命令 | 用途 |
|---|---|
| `scripts/dev.ps1` | 环境自检（解释器、依赖、Docker、Redis） |
| `scripts/smoke_world.py` | 被诊断系统冒烟（18/18 通过才算链路是好的） |
| `scripts/inject_fault.py` | 注入六种故障之一并记录变更事件 |
| `scripts/reduce_run.py` | 遥测降维验证（产出压缩率证据） |
| `scripts/mutate_check.py` | **变异检查**：证明回归用例真的能变红（`--list` 看所有变异组） |
| `scripts/hunt_world_mutators.py` | **查证"是谁把世界弄脏的"**：跑集成文件统计改动来源（匿名必须为 0），并做**阳性对照**（并发改旋钮时必须被指认出来）—— ADR-0009 / harness-log #65 |
| `scripts/exp_pool_vs_latency.py` | **对照实验台**：控制变量验证场景声明的标准答案是否成立（零 LLM 成本） |
| `eval/runner.py` | 评测运行器（`--agent baseline|multi`） |
| `eval/rescore.py` | 用新评分规则**离线重算**历史结果（不花钱；同时用于检查"有没有误伤"） |

所有 Python 脚本**必须**用 `.\.venv\Scripts\python.exe` 或 `uv run python` 执行。

---

## ✅ 第 6 条：完成判定的标准

一条错误**只有在回归用例能变红之后**，才算封堵。
一个功能**只有在验收标准可被第三方复现之后**，才算完成。

第三方复现的门槛：**陌生人克隆仓库 → 一条命令起全栈 → 复现全部数字**（含无 API Key 的离线回放）。

---

## 📝 第 7 条：文档纪律（防止"记忆失真"）

### 为什么要这条

对话上下文会随长度增长而失真；**文件不会**。
所以状态必须外置到文件，而不是留在对话里。

**但"多写文档"本身会导致另一种病**——同一个数字散落在多处，互不可校验。

> 真实教训：另一个项目里同一件事在 4 份文档里各有一份快照，
> 最后连"测试到底有多少个"都说不清。**不要重犯。**

### 规则

1. **每个领域只有一个权威文档。** 见 `docs/00-状态.md` §5 的文档地图。
2. **`docs/00-状态.md` 是进度与状态的唯一权威。** 其他文档**不得复制**
   进度、计数、完成状态；要引用就写"见 `docs/00-状态.md`"。
3. **每完成一个里程碑**必须：更新状态文档 → 追加 ADR（若有决策）→
   追加 harness-log（若有错误被封堵）→ 提交，并在提交信息里写里程碑编号。
4. **新会话/新代理进场**：先读 `AGENTS.md` → 再读 `docs/00-状态.md` → 再动手。
5. **设计先于编码。** 任何有设计含量的东西，先写进 `docs/`，确认后再写代码。
   目的是"一遍成"——返工的代价远大于多写一份设计。
6. **代码注释里写"为什么"，文档里写"设计与理由"，状态文档里写"在哪"。**
   三者不重复。
