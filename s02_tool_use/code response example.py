# -- Active: 1731326429981@@127.0.0.1@3306
# -- Active: 1731326429981@@127.0.0.1@330681@@127.0.0.1@3306
#!/usr/bin/env python3
"""
s02: Tool Use — 在 s01 基础上新增 4 个工具 + 分发映射。

运行: python s02_tool_use/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY

本文件 = s01 的全部代码 + 以下新增:
  + run_read / run_write / run_edit / run_glob 四个工具实现
  + TOOL_HANDLERS 分发映射（替代 s01 中硬编码的 run_bash 调用）
  + safe_path 路径安全校验

循环本身（agent_loop）与 s01 完全一致。
"""

import json, os, subprocess
from datetime import datetime
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


# ═══════════════════════════════════════════════════════════
#  FROM s01 (unchanged)
# ═══════════════════════════════════════════════════════════

def run_bash(command: str) -> str:
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


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 4 个新工具
# ═══════════════════════════════════════════════════════════
# 判断当前路径是否属于 WORKDIR 目录及其子目录。
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具定义（s01 只有一个 bash，现在扩展到 5 个）
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具分发映射（s01 是硬编码 run_bash，现在改为查表）
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s01 结构完全一致，只改了工具执行那部分
#  s01: output = run_bash(block.input["command"])
#  s02: output = TOOL_HANDLERS[block.name](**block.input)
# ═══════════════════════════════════════════════════════════

def _to_serializable(obj):
    """递归将 Anthropic SDK 对象转为可 JSON 序列化的字典/基本类型。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(v) for v in obj]
    return str(obj)


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # ── 循环结束，保存完整 Messages ──
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_dir = WORKDIR / "conversations" / f"trace_{timestamp}"
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / "messages.json").write_text(
                json.dumps(messages, indent=2, ensure_ascii=False, default=_to_serializable)
            )
            print(f"\n\033[32m✓ Messages saved to {save_dir / 'messages.json'}\033[0m")
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m> {block.name}\033[0m")
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})
#---------messages
# {'role': 'user', 'content': '当前目录下有什么文件'}
# {'role': 'assistant', 'content': [ThinkingBlock(signature='1c7e7656-2573-4ca9-a81c-83e3462511d7', 
# thinking='The user wants to know what files are in the current directory. Let me run a command to list them.', 
# type='thinking'), 
# ToolUseBlock(id='call_00_HAJXTDwGSzQ5WSfyVLCX6170', 
# caller=None, input={'command': 'ls -la'}, name='bash', type='tool_use')]}
# {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_00_HAJXTDwGSzQ5WSfyVLCX6170', 
# 'content': "'ls' �����ڲ����ⲿ���\ue8ecҲ���ǿ����еĳ���\n���������ļ���"}]}
# {'role': 'assistant', 'content': [ThinkingBlock(signature='110023a5-8772-4e57-95df-b91d023044e5', 
# thinking="The command didn't work properly, probably because of encoding issues with the Windows command prompt." \
# " Let me try using PowerShell or the full path.", type='thinking'), 
# ToolUseBlock(id='call_00_Ttbh3PG1l11Sh4X1b36y0742', caller=None, input={'command': 'cmd /c "dir /b"'}, 
#              name='bash', type='tool_use')]}
#--------response
# response: Message(id='110023a5-8772-4e57-95df-b91d023044e5', 
# container=None, content=[ThinkingBlock(signature='110023a5-8772-4e57-95df-b91d023044e5', 
# thinking="The command didn't work properly, probably because of encoding issues with the Windows command prompt. " \
# "Let me try using PowerShell or the full path.", type='thinking'), 
# ToolUseBlock(id='call_00_Ttbh3PG1l11Sh4X1b36y0742', caller=None, input={'command': 'cmd /c "dir /b"'},
#               name='bash', type='tool_use')], 
#               model='deepseek-v4-flash', role='assistant', 
#               stop_details=None, stop_reason='tool_use', stop_sequence=None, type='message', 
#               usage=Usage(cache_creation=None, cache_creation_input_tokens=0, cache_read_input_tokens=512, 
# inference_geo=None, input_tokens=135, output_tokens=78, output_tokens_details=None, server_tool_use=None, 
# service_tier='standard'))

# 停止的时候 stop_reason='end_turn'
# Message(id='4be2c0bc-4025-4a3f-a7c0-06a574b0eb54', container=None, content=[
# ThinkingBlock(signature='4be2c0bc-4025-4a3f-a7c0-06a574b0eb54', thinking='The user is asking "Who are you?" Let me introduce myself.', 
# type='thinking'), TextBlock(citations=None, text='你好！我是 **Claude**，由 Anthropic 开发的 AI 助手。\n\n我在这里以**编码代理（Coding Agent）**
# 的身份运行，当前工作目录是 `D:\\code\\learn_claude_code\\learn-claude-code`。\n\n我可以帮助你：\n\n1. **编写、修改和调试代码**\n2. **阅读和分析文件
# **\n3. **运行命令和脚本**\n4. **回答技术问题**\n5. **完成各种开发相关的任务**\n\n我有访问文件系统、运行 shell 命令的工具，可以高效地协助你完成编程工作
# 。\n\n请问有什么我可以帮你的吗？😊', type='text')], model='deepseek-v4-flash', role='assistant', stop_details=None, stop_reason='end_turn', st
# op_sequence=None, type='message', usage=Usage(cache_creation=None, cache_creation_input_tokens=0, cache_read_input_tokens=0, inference_geo=N
# one, input_tokens=535, output_tokens=140, output_tokens_details=None, server_tool_use=None, service_tier='standard'))

if __name__ == "__main__":
    print("s02: Tool Use — 在 s01 基础上加了 4 个工具")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
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
