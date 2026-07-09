"""SubAgentTool — 把聚焦子任务委派给独立子 Agent

让主 Agent 的 LLM 可以把「局部搜索 / 读取并总结 / 独立验证」等子任务交给一个
独立的子 Agent 执行。子 Agent 有自己的上下文，其中间过程（多步工具调用）不会
进入主对话——主 Agent 只看到一句结论，从而压低噪音、实现任务拆分。
"""

from typing import Any

from tools.base import Tool, ToolResult


class SubAgentTool(Tool):
    """委派子任务给独立子 Agent，返回其结论。"""

    name = "delegate_subtask"
    description = (
        "把一个聚焦、自洽的子任务（如局部搜索、读取并总结若干文件、独立验证某个改动）"
        "委派给子 Agent 执行，返回它的最终结论。子 Agent 拥有独立上下文，其中间步骤不会"
        "进入主对话——适合在主任务里压缩噪音或并行拆解。请在 task 里写清完整背景，"
        "因为子 Agent 看不到主对话历史。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "交给子 Agent 的完整、自洽的子任务描述（含必要背景）",
            }
        },
        "required": ["task"],
    }

    def __init__(
        self,
        llm: Any,
        workspace_root: str = ".",
        max_steps: int = 15,
        name: str = "subagent",
        parent: Any = None,
    ) -> None:
        self._llm = llm
        self._workspace_root = workspace_root
        self._max_steps = max_steps
        self._name = name
        # 父 Agent 引用：让子 Agent 继承其确认/详尽/auto 的安全姿态
        # （存引用即可，调用时父 Agent 已完成初始化）。
        self._parent = parent

    def run(self, **kwargs: Any) -> ToolResult:
        task = kwargs.get("task")
        if not task:
            return ToolResult.fail("缺少 task 参数：请提供要委派的子任务描述")

        # 局部导入避免与 coding_agent 形成模块级循环依赖
        from .coding_agent import CodingAgent

        # 继承父 Agent 的安全姿态：父级要求确认 / 已进入 auto 模式时，
        # 子 Agent 同样在写文件、运行命令前请求确认，避免「委派即绕过确认」。
        parent = self._parent
        require_confirm = bool(getattr(parent, "require_confirm", False))
        verbose = bool(getattr(parent, "verbose", False))
        auto_approve = bool(getattr(parent, "_auto_approve", False))

        sub = CodingAgent(
            name=self._name,
            llm=self._llm,
            workspace_root=self._workspace_root,
            max_steps=self._max_steps,
            enable_subagent=False,  # 禁止再嵌套，杜绝无限递归
            require_confirm=require_confirm,
            verbose=verbose,
            auto_approve=auto_approve,
        )
        try:
            answer = sub.run(task)
        except Exception as exc:
            return ToolResult.fail(
                f"子 Agent 执行失败：{type(exc).__name__}: {exc}"
            )
        # 若子 Agent 内用户选了 auto，把该选择回传父级，后续不再反复询问。
        if parent is not None and getattr(sub, "_auto_approve", False):
            parent._auto_approve = True
        return ToolResult.succeed(
            answer, subagent=self._name, sub_history=len(sub.get_history())
        )
