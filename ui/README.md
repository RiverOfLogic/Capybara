# Capybara 交互式 TUI 前端（Ink + React）

`capybara`（不带任务参数）时进入的终端 UI。Python 端（[`agents/__main__.py`](../agents/__main__.py) 的 `_launch_tui`）用当前解释器把本前端作为子进程拉起，前端再用**同一个** Python 解释器拉起后端 [`agents/tui_backend.py`](../agents/tui_backend.py)，两者通过 stdio JSON-lines 协议通信。

## 一次性设置

```bash
cd ui
npm install
```

需要 **Node.js ≥ 18**。安装后回到仓库任意位置直接：

```bash
capybara                 # 进入交互式 UI
capybara "读 README"      # 一次性模式（纯 Python，不经过本前端）
```

## 结构（声明式组件）

| 文件 | 职责 |
|------|------|
| `index.js` | 入口：解析参数、挂载前打印 Sixel logo、`render(<App/>)` |
| `protocol.js` | 拉起后端子进程、按行收发 JSON 协议 |
| `app.js` | 根组件 `<App>`：状态（reducer）、后端连接、事件→action 声明式映射、命令路由 |
| `components.js` | 展示组件：`Header` / `Transcript`(Static) / `Activity`(spinner 闪现) / `DiffView`(红绿 diff) / `SlashSuggestions`(自动补全) / `ConfirmDialog` / `PromptBox` / `HintBar`；`THEME` 统一配色 |
| `commands.js` | 斜杠命令注册表（单一来源，驱动 `/help` 与自动补全；标注 client / backend） |
| `i18n.js` | 双语文案表 + `t(key, lang, vars)`；只本地化 UI chrome |

## 协议

见 [`agents/tui_backend.py`](../agents/tui_backend.py) 顶部 docstring 或 [`docs/api-handbook.md`](../docs/api-handbook.md) §16。
新增三条：前端→后端 `{"type":"command","name":...}`（`/compact` 等后端类命令）；后端→前端
`{"type":"notice","format":"plain|diff","title":...,"text":...}`（命令结果 / 本轮改动，`diff`
格式由 `DiffView` 彩色渲染）；后端→前端 `{"type":"usage","used","window","percent"}`（上下文
token 用量，就绪 / 每轮结束 / `/compact` / `/reset` 后发，前端 `Header` 显示百分比）。

## 命令

输入 `/` 时下方弹出**自动补全**：`↑↓` 选择（列表随高亮滚动）、`Tab` 补全、`Enter` 执行；
提交的命令会在转录里**回显**（`❯ /skills`）。

| 命令 | 说明 | 处理端 |
|------|------|--------|
| `/help` | 显示命令帮助 | 前端 |
| `/clear` | 清屏（对话记忆保留） | 前端 |
| `/reset` | 清空对话历史（后端记忆一并清空） | 前端+后端 |
| `/compact` | 主动压缩对话历史（折叠较早轮次为摘要） | 后端 |
| `/skills` | 列出技能库（`skills_dir/*.md`） | 后端 |
| `/diff` | 重看上一轮改动（红绿 diff） | 后端 |
| `/codeintel` | ctags / tree-sitter / FTS5 就绪状态 | 后端 |
| `/trace` | 导出当前 trace 的 HTML 报告 | 后端 |
| `/lang [zh\|en]` | 切换界面语言（缺省则中/英互切） | 前端 |
| `/exit`（`/quit`） | 退出 | 前端 |

## 双语与稳定性

- **双语**：`/lang` 实时切换 UI chrome 的中/英文（`i18n.js`）。Agent 的回答、diff 内容、
  技能列表等**数据**不本地化，跟随对话 / 由后端产出。
- **红绿 diff**：删除行红、新增行绿、`@@`/文件头青；超 ~40 行自动省略中段。确认框里的
  写文件预览与每轮结束的改动都走同一个 `DiffView`。
- **终端稳定**：转录用 `<Static>` 不重绘；动态区用 Ink 的 `wrap="truncate-end"` 做宽度感知
  截断，中英文/CJK 宽字符混排不错行；补全弹层限高 + 滚动窗口跟随高亮。
- **上下文用量**：`Header` 实时显示 `上下文 N%`（token 估算 / `context_window`），≥70% 变黄、
  ≥90% 变红，提示该 `/compact` 了。

## 调试

后端 stderr 默认被 `protocol.js` 忽略以保持 UI 干净。排查后端问题时，把 `protocol.js`
里 spawn 的 `stdio` 第三项从 `'ignore'` 临时改为 `'inherit'`，即可看到后端的 traceback。
