"""M1：最小可运行的数据分析 Agent（只接工具，不带界面）

作用：你用中文问一句话 → 模型自己决定查什么 → 程序执行 SQL → 模型用人话回答

运行前准备：
    1) pip install openai
    2) 设置 API Key（二选一）
       · 临时：在 PowerShell 里执行  $env:DEEPSEEK_API_KEY="sk-你的Key"
       · 永久：把 Key 写进 .env，然后用 python-dotenv 读取（参考你项目 A 的做法）
    3) 确认下面的 DB_PATH 指向你下载的 Chinook 数据库

运行：
    python m1_minimal_agent.py

预期效果：
    你问> 消费最高的 5 位客户是谁？
    [第1步] 调用 get_schema ...
    [第1步] 调用 run_sql 参数={'query': 'SELECT ...'}
    最终回答：消费最高的客户是 Helena Holý，共消费 49.62 ...
"""

from __future__ import annotations

import json
import os
import sqlite3

from openai import OpenAI

# ------------------------------------------------------------------ 配置
DB_PATH = r"D:\data_agent\data\chinook.db"      # ← 改成你的实际路径
MODEL = "deepseek-chat"
MAX_STEPS = 5                                    # 步数上限，防止无限循环

# 优先读取项目目录下的 .env（和项目 A 的习惯一致），读不到再用系统环境变量
try:
    from dotenv import load_dotenv

    load_dotenv()          # 默认读取当前工作目录下的 .env
except ImportError:        # 没装 dotenv 也能用，走系统环境变量即可
    pass

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)


# ------------------------------------------------------------------ 工具 1：看表结构
def get_schema() -> str:
    """返回所有表名 + 字段名，让模型知道数据库里有什么。

    注：这里用 PRAGMA 是我们自己的代码（可信），不是模型生成的 SQL。
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)   # mode=ro = 只读
    lines: list[str] = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [f"{c[1]}({c[2]})" for c in conn.execute(f"PRAGMA table_info({table})")]
        lines.append(f"{table}: " + ", ".join(cols))
    conn.close()
    return "\n".join(lines)


# ------------------------------------------------------------------ 工具 2：执行 SQL
BANNED = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "ATTACH", "PRAGMA", "CREATE")


def run_sql(query: str) -> str:
    """执行模型生成的 SQL，带三重安全护栏。

    护栏：
      1) 只读连接（物理上写不进去）
      2) 白名单：只允许 SELECT / WITH 开头
      3) 禁止关键字 + 自动加 LIMIT
    出错时不抛异常，而是把错误信息返回给模型，让它自己改（这是 Agent 的关键）
    """
    q = query.strip().rstrip(";").strip()
    upper = q.upper()

    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return "拒绝执行：只允许 SELECT 查询。"
    for kw in BANNED:
        if kw in upper:
            return f"拒绝执行：SQL 中包含禁止的关键字 {kw}。"
    if "LIMIT" not in upper:
        q += " LIMIT 50"                      # 防止一次拉回整张表

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(q).fetchall()
    except Exception as exc:                   # SQL 写错了：把错误还给模型
        return f"SQL 执行出错：{exc}"
    finally:
        conn.close()

    return json.dumps(rows[:50], ensure_ascii=False, default=str)
    # default=str 是为了让日期等类型也能序列化


# ------------------------------------------------------------------ 告诉模型有哪些工具
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "查看数据库里所有表及其字段。写 SQL 之前先调用它。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "在只读数据库上执行一条 SELECT 查询，返回结果行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "一条 SQLite SELECT 语句"}
                },
                "required": ["query"],
            },
        },
    },
]

FUNCS = {"get_schema": get_schema, "run_sql": run_sql}


# ------------------------------------------------------------------ Agent 循环（核心就这段）
def solve(question: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "你是数据分析助手。流程：先调用 get_schema 看清表结构，"
                "再写 SELECT 查询获取数据，最后用中文总结结论。"
                "所有数值必须来自 SQL 查询结果，不要自己心算。"
            ),
        },
        {"role": "user", "content": question},
    ]

    for step in range(1, MAX_STEPS + 1):
        # ① 把对话发给模型，允许它调用工具
        reply = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS
        )
        msg = reply.choices[0].message
        messages.append(msg)          # 记录模型的决策（若报错可改为 msg.model_dump()）

        # ② 模型没要求调工具 → 说明它给出最终答案了
        if not msg.tool_calls:
            return msg.content or "(模型没有返回内容)"

        # ③ 模型要求调工具 → 我们真正执行
        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            print(f"[第{step}步] 调用 {name}  参数={json.dumps(args, ensure_ascii=False)[:150]}")

            try:
                result = FUNCS[name](**args)
            except KeyError:
                result = f"没有名为 {name} 的工具"

            print(f"         返回：{str(result)[:200]}")
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": str(result)}
            )

    return "达到步数上限，未能给出最终答案。"


def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("未找到 DEEPSEEK_API_KEY，请先设置环境变量。")
        return
    print("Chinook 数据分析 Agent（输入 exit 退出）\n")
    while True:
        try:
            question = input("你问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return
        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"} or question == "退出":
            print("已退出。")
            return
        try:
            print("\n最终回答：", solve(question), "\n")
        except Exception as exc:
            print(f"[错误] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
