#!/usr/bin/env python3
"""
s02: CC 风格工具定义 — 对比教学版的 TOOLS + TOOL_HANDLERS

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
教学版 (code.py)                         CC 风格 (本文件)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOLS = [...]                            buildTool(name=..., schema=..., execute=...)
TOOL_HANDLERS = {...}                    getAllBaseTools() → [Tool, Tool, ...]
定义和实现分离                            工具是自包含对象：schema + 验证 + 执行一体
无元数据                                  每个工具带 is_read_only / is_concurrency_safe / max_result_size
串行执行                                  按"连续并发安全块"分批，batch 内并发、batch 间串行
工具名→handler 查表分发                   结构化流水线：validate → pre_hook → execute
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

运行: python s02_tool_use/cc_style.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess, threading
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# ═══════════════════════════════════════════════════════════════════════════
#  核心抽象：Tool 类 + buildTool 工厂
# ═══════════════════════════════════════════════════════════════════════════

class Tool:
    """一个自包含的工具对象。

    CC 里的每个工具都是一个独立对象，包含：
      - schema: 发给 API 的工具定义（name / description / input_schema）
      - execute: 实际执行逻辑
      - validate: 执行前的参数校验
      - is_read_only / is_concurrency_safe: 并发调度和权限判断的元数据
      - max_result_size: 结果截断阈值

    教学版的 TOOLS 数组只包含 schema，TOOL_HANDLERS 只包含 execute。
    这里把它们合为一体。
    """

    def __init__(self, *, name, description, input_schema, execute,
                 validate=None, is_read_only=False,
                 is_concurrency_safe=None, max_result_size=50000):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._execute = execute
        # 校验函数：接收 **input，返回 None（通过）或错误字符串
        self._validate = validate or (lambda **kwargs: None)
        self.is_read_only = is_read_only
        # 默认：is_concurrency_safe 等于 is_read_only
        # 但可以覆盖——例如 TaskCreate 改状态但写入不同文件，仍可并发
        self.is_concurrency_safe = is_concurrency_safe if is_concurrency_safe is not None else is_read_only
        self.max_result_size = max_result_size

    # ── 给 API 用的工具定义（替代教学版 TOOLS 数组的条目） ──
    def to_api_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    # ── 验证流水线（CC 的 validateInput 阶段） ──
    def validate_input(self, **input) -> str | None:
        """校验参数。返回 None 表示通过，返回字符串表示错误。"""
        return self._validate(**input)

    # ── 执行（CC 的 tool.call() 阶段） ──
    def execute(self, **input) -> str:
        return self._execute(**input)

    def __repr__(self):
        return f"Tool({self.name}, read_only={self.is_read_only}, concurrent={self.is_concurrency_safe})"


def buildTool(*, name, description, input_schema, execute,
              validate=None, is_read_only=False,
              is_concurrency_safe=None, max_result_size=50000) -> Tool:
    """工厂函数：创建自包含的工具对象。

    相当于 CC 源码中的 buildTool()，它把 schema、验证、执行打包成一个对象。
    教学版的等价写法是：
        TOOLS.append({"name": name, ...})
        TOOL_HANDLERS[name] = execute
    但 CC 不分开——工具的所有信息都在一个对象里。
    """
    return Tool(
        name=name, description=description, input_schema=input_schema,
        execute=execute, validate=validate,
        is_read_only=is_read_only,
        is_concurrency_safe=is_concurrency_safe,
        max_result_size=max_result_size,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  路径安全（与教学版共用）
# ═══════════════════════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# ═══════════════════════════════════════════════════════════════════════════
#  逐个定义工具 —— CC 风格：每个工具都是独立 buildTool() 调用
# ═══════════════════════════════════════════════════════════════════════════

# ── 1. bash ────────────────────────────────────────────────────────────────

def _validate_bash(command="", **_) -> str | None:
    if not command or not command.strip():
        return "Error: empty command"
    return None


def _execute_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def _bash_concurrency_safe(command: str, **_) -> bool:
    """CC 精髓：按具体输入判断并发安全，不是按工具名一刀切。

    bash 本身可读可写，但 "ls" 和 "rm" 的并发安全性不同。
    教学版没法表达这种粒度——它只按工具名分发。
    """
    # 只读命令列表：这些命令不会改变状态，可以并发执行
    read_only_prefixes = (
        "ls", "cat", "echo", "head", "tail", "wc", "grep", "find",
        "pwd", "which", "type", "dir", "print", "git status",
    )
    cmd = command.strip().split()[0] if command.strip() else ""
    return any(cmd.startswith(prefix) for prefix in read_only_prefixes)


bash_tool = buildTool(
    name="bash",
    description="Run a shell command.",
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
    execute=_execute_bash,
    validate=_validate_bash,
    is_read_only=False,
    # is_concurrency_safe 是函数而非 bool——根据输入动态判断
    # 这对应 CC 中 isConcurrencySafe(input) 的设计
    is_concurrency_safe=_bash_concurrency_safe,
)

# ── 2. read_file ───────────────────────────────────────────────────────────

def _validate_read(path="", **_) -> str | None:
    try:
        safe_path(path)
    except (ValueError, Exception) as e:
        return f"Error: {e}"
    return None


def _execute_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


read_file_tool = buildTool(
    name="read_file",
    description="Read file contents.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    },
    execute=_execute_read,
    validate=_validate_read,
    is_read_only=True,
    is_concurrency_safe=True,
    # CC 特殊设计：FileRead 的 max_result_size 设为无穷大
    # 防止读文件的结果又被当成文件落盘 → 无限循环
    max_result_size=float('inf'),
)

# ── 3. write_file ──────────────────────────────────────────────────────────

def _validate_write(path="", content="", **_) -> str | None:
    try:
        safe_path(path)
    except (ValueError, Exception) as e:
        return f"Error: {e}"
    return None


def _execute_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


write_file_tool = buildTool(
    name="write_file",
    description="Write content to a file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    execute=_execute_write,
    validate=_validate_write,
    is_read_only=False,
    is_concurrency_safe=False,
)

# ── 4. edit_file ───────────────────────────────────────────────────────────

def _validate_edit(path="", old_text="", **_) -> str | None:
    try:
        safe_path(path)
    except (ValueError, Exception) as e:
        return f"Error: {e}"
    if not old_text:
        return "Error: old_text is required"
    return None


def _execute_edit(path: str, old_text: str, new_text: str = "") -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


edit_file_tool = buildTool(
    name="edit_file",
    description="Replace exact text in a file once.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text"],
    },
    execute=_execute_edit,
    validate=_validate_edit,
    is_read_only=False,
    is_concurrency_safe=False,
)

# ── 5. glob ────────────────────────────────────────────────────────────────

def _validate_glob(pattern="", **_) -> str | None:
    if not pattern or not pattern.strip():
        return "Error: pattern is required"
    return None


def _execute_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


glob_tool = buildTool(
    name="glob",
    description="Find files matching a glob pattern.",
    input_schema={
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
    execute=_execute_glob,
    validate=_validate_glob,
    is_read_only=True,
    is_concurrency_safe=True,
)


# ═══════════════════════════════════════════════════════════════════════════
#  汇总 —— 替代教学版的 TOOLS + TOOL_HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

def getAllBaseTools() -> list[Tool]:
    """汇总所有基础工具。

    教学版需要维护两个数据结构：
        TOOLS = [...]           ← 给 API 看
        TOOL_HANDLERS = {...}   ← 自己查表

    CC 只维护一个：
        getAllBaseTools()       ← 既能生成 API 定义，又能查表执行
    """
    return [
        bash_tool,
        read_file_tool,
        write_file_tool,
        edit_file_tool,
        glob_tool,
    ]


def find_tool(name: str) -> Tool | None:
    """按名称查找工具（替代 TOOL_HANDLERS[name] 查表）。"""
    for tool in getAllBaseTools():
        if tool.name == name:
            return tool
    return None


def get_api_definitions() -> list[dict]:
    """生成发给 API 的工具定义数组（替代 TOOLS 数组）。"""
    return [tool.to_api_definition() for tool in getAllBaseTools()]


# ═══════════════════════════════════════════════════════════════════════════
#  并发分批 —— partition_tool_calls
# ═══════════════════════════════════════════════════════════════════════════

def partition_tool_calls(blocks: list) -> list[list]:
    """将工具调用按"连续并发安全块"分批。

    CC 的 partitionToolCalls() 算法：
    - 从左到右扫描工具调用
    - 连续 is_concurrency_safe == true 的编入同一个 batch（可并发执行）
    - 遇到 false 的单独成一个 batch（串行执行）
    - 其后的 safe 工具重新开新 batch
    - batch 之间严格顺序

    教学版不做分批——按原始顺序逐个执行。

    示例：
        [read A, read B, glob *.py, bash "rm x", read C]
        → batch1(并发): [read A, read B, glob *.py]
        → batch2(串行): [bash "rm x"]
        → batch3(并发): [read C]
    """
    batches = []
    current_batch = []

    for block in blocks:
        if block.type != "tool_use":
            continue

        tool = find_tool(block.name)
        # is_concurrency_safe 可以是 bool 或函数
        raw = tool.is_concurrency_safe if tool else False
        safe = raw(**block.input) if callable(raw) else bool(raw)

        if safe:
            current_batch.append(block)
        else:
            # 关掉上一个 safe batch
            if current_batch:
                batches.append(current_batch)
                current_batch = []
            # 非并发安全的单独一批
            batches.append([block])

    if current_batch:
        batches.append(current_batch)

    return batches


# ═══════════════════════════════════════════════════════════════════════════
#  工具执行流水线 —— validate → permission → execute
# ═══════════════════════════════════════════════════════════════════════════

def execute_tool(tool: Tool, block) -> dict:
    """执行单个工具调用的完整流水线。

    CC 的 5 步验证管线（toolExecution.ts）：
      1. Zod schema 验证        ← 这里用 Python 原生类型检查
      2. tool.validateInput()   ← validate_input()
      3. PreToolUse hooks       ← (s04 介绍，这里用占位)
      4. 权限检查                ← (s03 的核心，这里用占位)
      5. tool.call()            ← execute()
    """
    # 步骤 2: 参数校验
    error = tool.validate_input(**block.input)
    if error:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": error,
        }

    # 步骤 3: PreToolUse hooks 占位（s04 会实现）
    # 钩子可以修改输入、阻止执行、或返回中间消息

    # 步骤 4: 权限检查占位（s03 会实现）
    # canUseTool + checkPermissions → allow / deny / ask

    # 步骤 5: 实际执行
    print(f"\033[33m> {tool.name} (concurrent={tool.is_concurrency_safe(**block.input) if callable(tool.is_concurrency_safe) else tool.is_concurrency_safe})\033[0m")
    output = tool.execute(**block.input)
    print(str(output)[:200])

    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": output,
    }


def execute_tool_calls(blocks: list) -> list[dict]:
    """执行一批工具调用——支持并发分批。

    教学版：按顺序逐个执行
        for block in response.content:
            handler = TOOL_HANDLERS[block.name]
            output = handler(**block.input)

    CC 版：分批 + batch 内并发执行
        batches = partition_tool_calls(blocks)
        for batch in batches:
            parallel execute all in batch
    """
    results = []
    batches = partition_tool_calls(blocks)

    print(f"\033[90m→ {len(blocks)} 个工具调用，分 {len(batches)} 批执行\033[0m")

    for i, batch in enumerate(batches):
        concurrent = len(batch) > 1
        print(f"\033[90m  第 {i + 1} 批: {[b.name for b in batch]} "
              f"{'(并发)' if concurrent else '(串行)'}\033[0m")

        if concurrent:
            # batch 内并发执行
            threads = []
            batch_results = [None] * len(batch)

            def run_in_thread(idx, blk):
                tool = find_tool(blk.name)
                batch_results[idx] = execute_tool(tool, blk) if tool else {
                    "type": "tool_result",
                    "tool_use_id": blk.id,
                    "content": f"Unknown tool: {blk.name}",
                }

            for idx, block in enumerate(batch):
                t = threading.Thread(target=run_in_thread, args=(idx, block))
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            results.extend(batch_results)
        else:
            # batch 内串行执行（只一个工具，不用开线程）
            block = batch[0]
            tool = find_tool(block.name)
            if tool:
                results.append(execute_tool(tool, block))
            else:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Unknown tool: {block.name}",
                })

    return results


# ═══════════════════════════════════════════════════════════════════════════
#  agent_loop — 与 s02 结构一致，但工具执行部分改为分批并发
# ═══════════════════════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        # 传给 API 的 tools 参数——直接从 getAllBaseTools() 生成
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=get_api_definitions(), max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        # CC 风格：分批 + 并发执行
        results = execute_tool_calls(response.content)
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════════════════════
#  __main__ — 交互式运行 + 展示 CC 风格的核心差异
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--demo" in sys.argv:
        # ── 展示 CC 风格和教学版的差异 ──
        print("=" * 60)
        print("CC 风格 vs 教学版 — 工具定义对比")
        print("=" * 60)
        print()

        tools = getAllBaseTools()

        print("1. 每个工具是自包含对象：")
        print("-" * 40)
        for t in tools:
            print(f"   {t}")
        print()

        print("2. 工具自带验证逻辑：")
        print("-" * 40)
        for t in tools:
            err = t.validate_input()
            print(f"   {t.name}.validate() → \"{err}\"")
        print()

        print("3. is_concurrency_safe 按输入动态判断（CC 特色）：")
        print("-" * 40)
        safe_cases = [
            ("bash", {"command": "ls -la"}),
            ("bash", {"command": "cat README.md"}),
            ("bash", {"command": "rm -rf /tmp/x"}),
            ("read_file", {"path": "README.md"}),
            ("write_file", {"path": "test.txt", "content": "hello"}),
        ]
        for name, inp in safe_cases:
            tool = find_tool(name)
            raw = tool.is_concurrency_safe
            safe = raw(**inp) if callable(raw) else bool(raw)
            print(f"   {name}({list(inp.keys())}) → {'并发安全' if safe else '需串行'}")
        print()

        print("4. partition_tool_calls 分批示例：")
        print("-" * 40)
        from anthropic.types import ToolUseBlock

        mock_blocks = [
            ToolUseBlock(id="call_1", name="read_file", input={"path": "a.py"}, type="tool_use"),
            ToolUseBlock(id="call_2", name="read_file", input={"path": "b.py"}, type="tool_use"),
            ToolUseBlock(id="call_3", name="glob", input={"pattern": "*.py"}, type="tool_use"),
            ToolUseBlock(id="call_4", name="bash", input={"command": "rm -rf tmp"}, type="tool_use"),
            ToolUseBlock(id="call_5", name="read_file", input={"path": "c.py"}, type="tool_use"),
        ]
        batches = partition_tool_calls(mock_blocks)
        for i, batch in enumerate(batches):
            names = [b.name for b in batch]
            conc = len(batch) > 1
            print(f"   第 {i + 1} 批: {names} {'(并发 ⚡)' if conc else '(串行)'}")
        print()

        print("5. get_api_definitions() 输出（与教学版 TOOLS 一致）：")
        print("-" * 40)
        import json
        print(json.dumps(get_api_definitions(), indent=2, ensure_ascii=False))
        print()

        print("6. max_result_size 对比：")
        print("-" * 40)
        for t in tools:
            size_str = "∞" if t.max_result_size == float('inf') else str(t.max_result_size)
            print(f"   {t.name}: {size_str}")
        print()

        print("=" * 60)
        print("教学版:  TOOLS[5]  +  TOOL_HANDLERS[5]  =  2 个集合")
        print("CC 版:   5 × Tool(自包含)  +  1 个 getAllBaseTools() =  统一管理")
        print("=" * 60)

    else:
        # ── 交互模式（与 code.py 行为一致，但内部用 CC 风格执行） ──
        print("s02: CC 风格工具定义 — 对比教学版")
        print("每个工具是 buildTool() 创建的自包含对象。")
        print("输入问题，回车发送。输入 q 退出。\n")

        history = []
        while True:
            try:
                query = input("\033[36mcc >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            history.append({"role": "user", "content": query})
            agent_loop(history)
            for block in history[-1]["content"]:
                if getattr(block, "type", None) == "text":
                    print(block.text)
            print()
