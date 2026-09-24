"""M2：数据分析 Agent v2（在 M1 基础上加了 4 件事）

M1 → M2 的四项升级（每一项都对应 M1 实测发现的问题）：
    ① 加 plot_chart 工具 —— M1 里模型没有画图工具，竟然用字符"手绘"了一个假折线图
    ② 加系统提示约束 —— 禁止假造图表、禁止提供破坏性 SQL、歧义必须说明
    ③ 加多轮记忆 —— M1 里问"那只看 2010 年呢？"它接不上上文
    ④ 加相同错误检测 —— 同一个错误不要重复犯

运行前准备：
    1) pip install openai matplotlib
    2) 确认 DB_PATH 指向你的 chinook.db
    3) API Key 放在项目目录的 .env 里，或设置环境变量 DEEPSEEK_API_KEY

运行：
    python m2_agent.py

新增命令：
    输入 clear 清空对话记忆（重新开始一轮）
    输入 exit  退出
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")                      # 无界面后端：只保存图片文件，不弹窗
import matplotlib.pyplot as plt            # noqa: E402

from openai import OpenAI                  # noqa: E402

# ------------------------------------------------------------------ 配置
DB_PATH = r"D:\data_agent\data\chinook.db"
OUT_DIR = Path(r"D:\data_agent\outputs")   # 图表输出目录
MODEL = "deepseek-chat"
MAX_STEPS = 6

# 中文字体，否则图里中文会变方框
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)


# ================================================================== 安全层
BANNED = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "ATTACH", "PRAGMA", "CREATE")


def _check_sql(query: str) -> tuple[str | None, str | None]:
    """SQL 安全校验。返回 (安全的SQL, 错误信息)，两者必有一个是 None。

    这是所有数据库操作的唯一入口 —— 集中校验，避免各处漏检。
    """
    q = query.strip().rstrip(";").strip()
    upper = q.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return None, "拒绝执行：只允许 SELECT 查询。"
    for kw in BANNED:
        if kw in upper:
            return None, f"拒绝执行：SQL 中包含禁止的关键字 {kw}。"
    return q, None


def _fetch(query: str) -> tuple[list[str] | None, list | None, str | None]:
    """执行只读查询，返回 (列名, 数据行, 错误信息)。"""
    q, err = _check_sql(query)
    if err:
        return None, None, err
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)   # mode=ro：只读
    try:
        cur = conn.execute(q)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as exc:                                       # SQL 写错
        return None, None, f"SQL 执行出错：{exc}"
    finally:
        conn.close()
    return cols, rows, None


# ================================================================== 工具层
def get_schema() -> str:
    """查看所有表及其字段。写 SQL 之前先调用。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    lines: list[str] = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [f"{c[1]}({c[2]})" for c in conn.execute(f"PRAGMA table_info({table})")]
        lines.append(f"{table}: " + ", ".join(cols))
    conn.close()
    return "\n".join(lines)


def run_sql(query: str) -> str:
    """执行 SELECT 查询，返回结果行。出错时把错误信息交回模型让它自己改。"""
    cols, rows, err = _fetch(query)
    if err:
        return err
    return json.dumps(rows[:50], ensure_ascii=False, default=str)


def plot_chart(
    query: str,
    x_field: str,
    y_field: str,
    chart_type: str = "line",
    title: str = "",
) -> str:
    """执行 SQL 并生成图表，返回 PNG 文件路径。

    ⭐ 关键设计：模型只传【SQL + 列名】，不传具体数值。
       因为让模型把数字一个个转录过来，很容易出错（数值幻觉）；
       让代码自己去数据库取数，才是可靠的。
    """
    cols, rows, err = _fetch(query)
    if err:
        return f"绘图失败：{err}"
    if not rows:
        return "绘图失败：查询没有返回任何数据。"
    if x_field not in cols or y_field not in cols:
        return f"绘图失败：列名不对。实际可用的列是：{cols}"

    xi, yi = cols.index(x_field), cols.index(y_field)
    xs = [str(r[xi]) for r in rows]
    ys = [float(r[yi]) if r[yi] is not None else 0.0 for r in rows]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    if chart_type == "bar":
        ax.bar(xs, ys, color="steelblue")
    elif chart_type == "pie":
        ax.pie(ys, labels=xs, autopct="%1.1f%%")
    else:
        ax.plot(xs, ys, marker="o", color="steelblue", linewidth=1.5)

    if chart_type != "pie":
        ax.set_title(title or f"{y_field} 按 {x_field} 变化")
        ax.set_xlabel(x_field)
        ax.set_ylabel(y_field)
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"chart_{time.strftime('%Y%m%d_%H%M%S')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return f"图表已保存到：{path}"


FUNCS = {"get_schema": get_schema, "run_sql": run_sql, "plot_chart": plot_chart}

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
    {
        "type": "function",
        "function": {
            "name": "plot_chart",
            "description": "执行 SQL 并把结果画成图表，保存为 PNG 文件。用户要求画图时必须用这个工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "取数的 SELECT 语句"},
                    "x_field": {"type": "string", "description": "作为横轴的列名"},
                    "y_field": {"type": "string", "description": "作为纵轴的列名"},
                    "chart_type": {
                        "type": "string",
                        "enum": ["line", "bar", "pie"],
                        "description": "图表类型，默认 line",
                    },
                    "title": {"type": "string", "description": "图表标题"},
                },
                "required": ["query", "x_field", "y_field"],
            },
        },
    },
]

# ================================================================== 提示词
SYSTEM_PROMPT = """你是数据分析助手，可以调用工具查询只读数据库。

必须遵守的规则：
1. 写 SQL 之前先调用 get_schema 了解真实表结构，不要凭空猜字段名。
2. 所有数值必须来自 run_sql 或 plot_chart 的返回结果，禁止自己心算或估计。
3. 用户要求画图时，必须调用 plot_chart 工具。禁止用字符拼图，禁止只输出代码让用户自己运行。
4. 如果查询结果存在并列、歧义、数据缺失或数据本身有局限（例如是示例数据集），必须主动说明。
5. 如果用户要求删除或修改数据，明确拒绝并说明本系统只能只读查询；不要提供可直接执行的破坏性 SQL。
6. 用中文回答；先给结论，再给依据；结论要基于数据。"""


# ================================================================== Agent 循环
def solve(question: str, messages: list, seen_errors: set) -> str:
    messages.append({"role": "user", "content": question})

    for step in range(1, MAX_STEPS + 1):
        reply = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS
        )
        msg = reply.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:                      # 模型给出最终答案
            used = getattr(reply, "usage", None)
            if used:
                print(f"         [本轮 token：{used.total_tokens}]")
            return msg.content or "(模型没有返回内容)"

        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            print(f"[第{step}步] {name}  {json.dumps(args, ensure_ascii=False)[:140]}")

            if name not in FUNCS:
                result = f"没有名为 {name} 的工具"
            else:
                result = FUNCS[name](**args)

            # 相同错误检测：同一个错误不要反复犯
            text = str(result)
            if "出错" in text or "拒绝执行" in text or "失败" in text:
                key = (name, text[:120])
                if key in seen_errors:
                    text += "\n（提示：你刚刚犯过完全相同的错误，请换一种写法重试。）"
                seen_errors.add(key)

            print(f"         返回：{text[:180]}")
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": text}
            )

    return "达到步数上限，未能给出最终答案。"


def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("未找到 DEEPSEEK_API_KEY，请先配置 .env 或环境变量。")
        return

    print("Chinook 数据分析 Agent v2（clear 清空记忆 / exit 退出）\n")
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    seen_errors: set = set()

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
        if question.lower() == "clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            seen_errors.clear()
            print("记忆已清空。\n")
            continue

        try:
            print("\n最终回答：", solve(question, messages, seen_errors), "\n")
        except Exception as exc:
            print(f"[错误] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
