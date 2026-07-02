<div align="center">

<img src="logo.png" alt="Capybara" width="320" />

# Capybara（卡皮巴拉）

**一个像卡皮巴拉一样冷静、可靠的 Coding Agent。**

[English](README.md) · [简体中文](README.zh-CN.md)

![Python ≥ 3.11](https://img.shields.io/badge/Python-≥3.11-4A90E2?style=flat-square)
![License MIT](https://img.shields.io/badge/License-MIT-4A90E2?style=flat-square)
![Status active](https://img.shields.io/badge/Status-active-4A90E2?style=flat-square)

</div>

---

## 是什么？

Capybara 是一个 Python 编程 Agent：把一条任务交给它，它会读代码、改文件、跑验证、出结果——靠 ReAct 循环自动调用一组精心挑选的 14 个工具，并自带对话历史和工具 trace 的工作记忆。设计上像它的同名动物那样稳重：不慌、不漂移、不偷偷改东西。

如果你用过 Claude Code 或 Aider，心智模型基本一致。区别在于 Capybara 是一个**看一眼就能读完**的库 + CLI——`core/` + `tools/` + `agents/` 合计三千行上下。

---

## 能做什么

一句话版：

- 读写代码 —— 列目录 / 读文件 / 搜索 / 单处 patch / 多处 patch / 按行号替换 / 整文件写
- 跑验证 —— 设 `verify_command`，自动循环 fix-verify 直到通过（最多 `max_fix_iters` 轮）
- 导航代码库 —— ctags 找定义、FTS5 全文搜、tree-sitter 找引用 + import 依赖图 + 仓库地图
- 接外部工具 —— MCP 客户端（stdio，零依赖）消费任意 Model Context Protocol server
- 自检 —— 指数退避重试、连续失败熔断、单次任务 token 预算
- 写文件 / 跑命令前先问 —— 每次改动弹确认框，可输入 `a` 进入 auto
- 流式输出 —— async 事件流给 UI 用；自带仿 Claude Code 风格的 Ink (React) 终端 UI
- 可观测 —— 可选 JSONL trace + HTML 报告；会话可保存 / 恢复，schema 漂移会告警
- 守住预算 —— 长历史自动压缩、单任务内 tool 分组折叠、工具输出截断

5 个代码智能工具（`find_definition` / `search_codebase` / `find_references` / `repo_map` / `get_file_relations`）**默认就在**——tree-sitter 后端那几个无需装 extras。

---

## 安装

### 前置依赖

| | 什么时候需要 | 安装方式 |
|---|---|---|
| **Python ≥ 3.11** | 任何时候 | python.org 安装包 / `pyenv install 3.11` |
| **Node.js ≥ 18** | **仅**交互式 TUI | nodejs.org LTS / `nvm install 18` |
| **Universal Ctags** | **仅** `find_definition` | `brew install universal-ctags` / `apt install universal-ctags` / `choco install universal-ctags` |

Node 和 ctags 缺哪个都不影响 Capybara 启动——它会在启动横幅里告诉你缺啥（见下文）。

### 方式 A：pipx（推荐）

pipx 把 Capybara 装进**自己的隔离环境**，装完后 `capybara` 命令在任何目录、任何 shell（PowerShell / cmd / bash）都能直接用——不用 `conda activate`，不用切 venv。

```bash
python -m pip install --user pipx
python -m pipx ensurepath                          # 把 pipx bin 加进 PATH；之后重开一个终端

# 在仓库根目录
pipx install --editable .

# 之后在任意目录
capybara "加一个 CLI 入口并跑测试验证" --workspace ./myproj
```

可编辑安装：改源码即时生效。只有改了 `pyproject.toml` 的依赖才需要 `pipx install --editable . --force`。

### 方式 B：装进当前环境

```bash
conda run -n <your-env> pip install -e .
# 或：  pip install -e .
```

只在那个 env 激活时 `capybara` 命令才在 PATH 上。

---

## 两件小配置

### 1. LLM 凭据 —— 工作目录的 `.env`

```bash
LLM_API_KEY="sk-..."                        # 任一 OpenAI 兼容 provider
LLM_BASE_URL="https://api.deepseek.com"     # DeepSeek / OpenAI / Qwen / Kimi / …
LLM_MODEL_ID="deepseek-chat"                # 实际要调的模型
LLM_TIMEOUT=60                              # 可选，秒
```

少任何一个会在 `AgentsLLM` 启动时直接抛——不会让你聊到一半才发现。

### 2. 项目配置 —— `./myproj/.capybara.toml`（可选）

单文件 TOML，`core/config.py::Config` 里的任何字段都可以在这里覆盖。CLI 自动在工作区根目录探测；用 `--config <path>` 指向别处也可以。仓库自带一份**全字段带注释**的 `.capybara.toml` 模板，原样照抄即可。

最小示例：

```toml
# 指向 ctags —— 留空则查系统 PATH
ctags_path = '/usr/local/bin/ctags'

# 调 Agent 的姿态
trace_enabled = true
context_window = 64000

# 接外部 MCP server —— 不用再在 Python 里传 mcp_servers=[...]
[[mcp_servers]]
name = "example"
command = "python"
args = ["path/to/your_mcp_server.py"]
```

未知字段 pydantic 会静默忽略，多写一行不会炸。

---

## 自带依赖（就这几个）

5 个 pip 包，全部带**上界**防止大版本漂移：

| 包 | 用途 |
|---|---|
| `openai>=2,<3` | LLM 客户端（兼容所有 OpenAI 格式服务：DeepSeek / Qwen / Kimi / OpenAI / Together / …） |
| `python-dotenv>=1,<2` | import 时自动加载 `.env` |
| `pydantic>=2,<3` | `Config` + `Message` 数据模型 |
| `tree-sitter>=0.25,<0.26` | 给 `find_references` / `repo_map` / `get_file_relations` 提供 AST |
| `tree-sitter-language-pack>=1.10,<2` | 150+ 语言的 tree-sitter grammar（Python / JS / TS / Go / Rust / Java / C / C++ / …） |

外加**一个外部二进制**（故意不做成 pip 依赖）：

- **Universal Ctags** —— 给 `find_definition` 用。唯一配置入口是 `.capybara.toml` 里的 `ctags_path` 字段，或者把 `ctags` 放进 `PATH`。

### 启动横幅

如果代码智能有一块缺席，Capybara 启动时**只往 stderr 打一条中文横幅**，告诉你缺哪块、怎么补：

```
⚠ 代码智能可用度不足（不影响基础读写/搜索/运行，仅以下高级检索降级）：
  · ctags 未找到 → find_definition 不可用。
      装 Universal Ctags：Windows `choco install universal-ctags` / `scoop install universal-ctags`；
      或在 .capybara.toml 设 ctags_path 指向 ctags 可执行文件。
```

Capybara **不会因此退出**——基础读写 / 搜索 / 运行照常工作，只是依赖那块的高级检索降级。全员到位时横幅静默，stdout 保持干净。

---

## 用法

### 一次性 CLI

```bash
# 最小用法
capybara "修 foo.py 的拼写 bug 并跑 compileall"

# 切换工作区，跳过确认
capybara "重构缓存模块" --workspace ./myproj --auto-approve

# 全栈生产预设 + 验证-修复闭环
capybara "接入 CLI 入口" --production --verify "python -m unittest"
```

参数：

- `--production` —— 开 trace / 自动保存 / 项目感知 / 改动追踪 / 子 Agent / 预算
- `--verify CMD` —— 改完先跑 `CMD`；挂了当 user 消息回灌，最多 `max_fix_iters` 轮
- `--auto-approve` —— 整轮跳过写文件 / 跑命令前的确认
- `--max-steps N` —— ReAct 步数上限（默认 20）
- `--config PATH` —— 用指定 `.capybara.toml`
- `--verbose` —— 把执行过程原样打到 stdout

### 交互式 TUI（仿 Claude Code）

不传任务参数直接 `capybara`，进入 Ink（React）终端 UI：启动打 Sixel logo、显示当前模型、底部输入框、多轮对话；工具调用"闪现一下"（spinner + 当前步骤，完成即收起），同时落一条紧凑记录在常驻转录里；写文件 / 跑命令前弹确认框（`y` 通过 / `n` 拒绝）。

```bash
cd ui && npm install     # 一次性
capybara                  # 在任意目录进入 UI
```

需要 Node ≥ 18。一次性 `capybara "任务"` 模式完全不用 Node。

### 当库用（Python）

```python
from core.llm import AgentsLLM
from agents import CodingAgent

agent = CodingAgent(name="coder", llm=AgentsLLM(), workspace_root=".")
print(agent.run("修 foo.py 拼写 bug 并用 compileall 验证"))
```

或者异步 + 流式：

```python
async for ev in agent.arun_stream("重构缓存模块"):
    print(ev.type, ev.data)
```

想要全栈（trace + 自动保存 + 项目感知 + 确认/auto + 改动追踪 + 子 Agent + 预算）一行起：

```python
agent = CodingAgent.production(
    name="coder", llm=AgentsLLM(), workspace_root=".",
    verify_command="python -m unittest",
)
```

---

## 测试

298 个 stdlib `unittest` 用例，无需 `pytest`，不发真实网络请求。代码智能相关用例在 ctags / tree-sitter 缺失时会自动 skip，所以刚 clone 下来的仓库也是能跑的：

```bash
python -m unittest discover -s tests
```

可跑的 demo（有的需要 LLM、有的不用）：

```bash
python demo/coding_agent_resilience_demo.py     # 重试 / 熔断 —— 不需要 LLM
python demo/coding_agent_orchestration_demo.py  # 子 Agent 委派 —— 不需要 LLM
python demo/coding_agent_mcp_demo.py            # MCP 客户端 —— 第一段离线可跑
python demo/coding_agent_pro_demo.py            # 全栈生产预设 —— 需要 LLM
```

---

## 进一步阅读

| 文档 | 内容 |
|---|---|
| `PROJECT.md` | 架构 / 包布局 / 完整变更记录 |
| `docs/api-reference.md` | 公开 API 按包索引，import 路径已经实跑验证 |
| `docs/api-handbook.md` | 内部机制 + 扩展点 + 排障菜谱 |
| `docs/code-intelligence.md` | 代码智能子系统的设计 |
| `docs/mcp-guide.md` | MCP 入门 + 自己写 server 的步骤 |
| `docs/enhancement-roadmap.md` | 当前仍在 TODO 的事项 |

---

## 状态与贡献

活跃维护中。路线图阶段 0–8 全部交付，其后四轮硬化（P0–P3 / CI-1–4 / 打包·MCP·编码）也都做了。真正的待办见 `docs/enhancement-roadmap.md`。

Issue 和 Discussion 是开的。发 PR 前先开 Issue 聊一下，`docs/agent-structure-guide.md` 列了代码约定。

---

## License

MIT —— 见 [LICENSE](LICENSE)。
