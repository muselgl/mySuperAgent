# mySuperAgent

个人研发 Superagent 的本地执行原型。

## Quick start

```powershell
python -m my_superagent.cli task "检查这个仓库并运行测试" --repo .
python -m my_superagent.cli task list
python -m my_superagent.cli task run <task-id>
python -m my_superagent.cli task show <task-id>

# Router/Worker service entry points
python -m my_superagent.cli router serve
python -m my_superagent.cli worker serve --worker-id worker-1
python -m my_superagent.cli supervisor serve
python -m my_superagent.cli watchdog serve
```

第一阶段使用标准库实现 Router/Worker 的任务状态、计划、工具执行和 WorkerReport。支持确定性 `LocalPlanner` 和模型驱动的 `ModelPlanner`。

## CLI 连续对话

配置好项目根目录的 `.env` 后，启动交互会话：

```powershell
python -m my_superagent.cli chat --repo .
```

启动时显示会话 ID 和工作目录。每行输入一个问题，Agent 根据对话历史回答；需要代码证据时，自主调用文件列表、文件读取或 Git 状态工具。

```text
你：分析项目启动流程，请引用代码行号
[工具] file.read {"path": "my_superagent/cli.py"}
Agent：启动流程如下……
你：展开解释刚才提到的模型调用
Agent：结合刚才读取的代码……
你：/exit
```

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看交互命令 |
| `/history` | 查看完整问答，以及失败、中断状态 |
| `/retry` | 使用最近一次失败、中断或预算耗尽的问题开启新一轮，携带已有证据并重新计算本轮预算 |
| `/exit` | 保存并退出，显示完整的恢复命令 |

每轮用户输入、模型动作、工具结果、回答和用量自动保存在 SQLite。退出后使用启动时显示的会话 ID 继续对话：

```powershell
python -m my_superagent.cli chat --resume <session-id>

# 使用指定的数据库或模型配置
python -m my_superagent.cli chat --resume <session-id> --db .superagent/state.db --env-file .env
```

恢复时沿用已保存的工作目录，并读取本次启动指定的模型配置。新会话的工作目录默认为当前目录，默认数据库为 `.superagent/state.db`。旧数据库在打开时自动增加聊天相关表。

输入阶段按 Ctrl+C 或遇到 EOF 会保存并退出；执行阶段按 Ctrl+C 会保存中断状态并回到输入提示。进程意外退出后，恢复会话会标记上次未完成的轮次，并提示使用 `/retry` 或继续提问。

每轮最多 20 次模型动作、30 分钟；单条输入最多 16000 字符。模型使用最多 80000 字符的对话上下文，始终保留当前问题，并保留最近历史，工具调用与结果成对保留。发生裁剪时终端显示省略数量，完整问答可通过 `/history` 回看。文件读取继续沿用下述工具的路径、大小与分页限制。

## 模型驱动体验

在项目根目录配置好 `.env` 后，用一条命令创建并执行任务：

```powershell
python -m my_superagent.cli task "阅读 README 和主要代码，说明项目执行流程，并给出三个改进建议。请引用实际文件路径和行号。" --repo . --planner model --watch
```

终端依次展示模型请求、计划、当前步骤、工具调用及执行结果，最后输出模型总结。`--watch` 在创建任务时立即启动执行；运行已有任务时展示实时进度。每个模型请求返回后继续执行下一步。

模型先生成 1～4 个调查步骤，执行器追加最终总结步骤。每个步骤可以多次调用工具；每次结果（含错误）都会保存并送入下一轮模型上下文，模型根据反馈调整动作。工具包括：

| 工具 | 用途 |
| --- | --- |
| `workspace.inspect` | 列出最多 200 个工作区文件，跳过缓存、依赖目录和常见凭据文件 |
| `file.read` | 按相对路径读取 UTF-8 文本，返回文件路径和行号；支持 `start_line`、`max_lines` 分页 |
| `git.status` | 读取 Git 工作区状态 |

首轮体验面向仓库阅读与分析。文件读取限制在工作区内，每次最多 500 行、16000 字符，文件大小上限为 2 MB。每个任务最多执行 20 次模型动作（包含工具调用和步骤总结），耗时预算为 30 分钟；预算耗尽时保存记录并标记为 `blocked`。最近最多 80000 字符的工具观察结果与所有已完成步骤的摘要进入模型上下文。

也可以分开创建、执行和回看：

```powershell
$taskId = python -m my_superagent.cli task "阅读 README.md 和 my_superagent/cli.py，解释启动流程并引用代码行号" --repo . --planner model
python -m my_superagent.cli task run $taskId --watch
python -m my_superagent.cli task show $taskId
```

规划器类型随任务保存，后续执行沿用该选择。`task show` 包含 `planner`、`final_summary`、计划、工具参数、工具结果和模型请求的用量记录。运行中断后，可以再次运行同一任务以读取已保存的观察结果并继续；最近一次尚未保存的模型动作会重新执行。

已有 SQLite 数据库在打开时自动补充规划器与总结字段。默认保存路径为 `.superagent/state.db`，可通过 `--db` 指定其他文件。`--env-file` 指定本次执行使用的模型配置文件，例如：

```powershell
python -m my_superagent.cli task run $taskId --watch --env-file .env
```

省略 `--planner` 创建任务时沿用 `LocalPlanner`。

## 模型连接配置

项目根目录的 `.env` 保存本地连接配置，进程环境变量具有更高优先级。配置示例见 [.env.example](.env.example)，`.env` 已加入 Git 忽略规则。

```dotenv
SUPERAGENT_API_KEY=your-api-key
SUPERAGENT_BASE_URL=https://ruoli.dev/v1
SUPERAGENT_MODEL=gpt-6.1-terra
SUPERAGENT_API_MODE=responses
SUPERAGENT_API_TIMEOUT_SECONDS=60
```

运行一次最小文本请求检查连接：

```powershell
python -m my_superagent.cli model check
```

模型客户端支持 `responses` 和 `chat_completions` 两种协议。`ModelConfig.load()` 读取配置，`ModelClient.generate(prompt)` 发起请求。`ModelPlanner` 复用文本请求通路，以经过校验的 JSON 生成计划与工具动作。

## 开发验证

```powershell
python -m pytest -q
```

自动测试使用模拟模型回复验证任务与连续对话的 CLI 执行链路、工具反馈、会话恢复、异常重试、上下文裁剪、预算控制和数据库迁移。真实服务可使用 `chat --repo .` 连续追问，或使用上面的 `--planner model --watch` 命令执行任务。

架构说明见 [docs/architecture.md](docs/architecture.md)。

## Router/Worker 基础能力

打开 SQLite 时会自动启用 WAL、外键检查和幂等迁移。状态库现在包含 Thread、Trigger、Run、Lease、Outbox、Artifact、Approval、订阅、依赖、检查点、Environment 和 Lock 表；旧数据库会保留原任务与聊天数据。

事件通过版本化 `EventEnvelope` 保存，`TaskStore.append_envelope()` 同时写入任务事件与 Outbox。`claim_task()` 使用事务和 fencing token 领取任务，`heartbeat()` 只续租，旧 token 的写入会被拒绝。Broker 通道有固定容量，满载时消息落入 Outbox。

常用控制命令：

```powershell
python -m my_superagent.cli task create "检查仓库" --repo .
python -m my_superagent.cli task events <task-id>
python -m my_superagent.cli task approve <task-id> <approval-id>
python -m my_superagent.cli task resume <task-id>
```
