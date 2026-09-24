"""M3：数据分析 Agent v3（修准 + 控本）

M2 → M3 的三项升级（每一项都对应观察日志里的一个真实发现）：

    ① 【修准】工具返回结构化数据：带列名 + 行数 + 截断标记
       —— 根因：M2 的 run_sql 只返回 [[412, 412, 0]]，没有列名，
          模型只能猜哪一列是什么，于是把"412 张发票"说成"412 位客户"。
          修数据格式 > 加提示词，这是代码层的修复。

    ② 【控本】上下文裁剪：只保留最近 N 轮 + 工具结果瘦身
       —— 根因：M2 实测 token 1874 → 3016 → 4605 → 5700 线性增长。
          注意：裁剪时必须"成对保留" assistant(tool_calls) 与 tool 消息，
          否则 API 会报错（这是新手最容易踩的坑）。

    ③ 【约束】提示词新增三条自检规则（针对数值误读）

运行：
    python m3_agent.py
命令：
    clear 清空记忆 ｜ trim 手动查看裁剪后的消息数 ｜ exit 退出
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt            # noqa: E402

from openai import OpenAI                  # noqa: E402

# ------------------------------------------------------------------ 配置
DB_PATH = r"D:\data_agent\data\chinook.db"
OUT_DIR = Path(r"D:\data_agent\outputs")
MODEL = "deepseek-chat"
MAX_STEPS = 6
MAX_HISTORY_TURNS = 3      # 只保留最近 3 轮问答
MAX_ROWS = 20              # 回灌给模型的最大行数
MAX_CELL = 200             # 单个单元格最大字符数

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
    q = query.strip().rstrip(";").strip()
    upper = q.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return None, "拒绝执行：只允许 SELECT 查询。"
    for kw in BANNED:
        if kw in upper:
            return None, f"拒绝执行：SQL 中包含禁止的关键字 {kw}。"
    return q, None


def _fetch(query: str):
    """执行只读查询，返回 (列名, 行, 错误)。"""
    q, err = _check_sql(query)
    if err:
        return None, None, err
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.execute(q)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as exc:
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


def _short(value) -> str:
    """超长单元格截断，避免吃 token。"""
    text = str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


def run_sql(query: str) -> str:
    """执行 SELECT，返回【带列名】的结构化结果。

    ⭐ M3 的核心修复：返回 {"columns": [...], "row_count": n, "rows": [[...]]}
       让模型明确知道每个数字属于哪一列，从根上消除"列含义误读"。
    """
    cols, rows, err = _fetch(query)
    if err:
        return err
    payload = {
        "columns": cols,
        "row_count": len(rows),                                  # 真实总行数
        "rows": [[_short(v) for v in row] for row in rows[:MAX_ROWS]],
        "truncated": len(rows) > MAX_ROWS,
    }
    return json.dumps(payload, ensure_ascii=False)


def plot_chart(
    query: str,
    x_field: str,
    y_field: str,
    chart_type: str = "line",
    title: str = "",
) -> str:
    """执行 SQL 并画图，返回文件路径 + 数据摘要（模型只传 SQL 和列名，不传数值）。"""
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
    return json.dumps(
        {
            "chart_path": str(path),
            "x_field": x_field,
            "y_field": y_field,
            "point_count": len(rows),
            "y_min": min(ys),
            "y_max": max(ys),
        },
        ensure_ascii=False,
    )


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
            "description": "执行 SELECT 查询。返回带列名的结构化结果：columns 是列名，rows 是数据行，row_count 是总行数。",
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
            "description": "执行 SQL 并画图保存为 PNG。用户要求画图时必须用这个工具，禁止用字符拼图或只输出代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "取数的 SELECT 语句"},
                    "x_field": {"type": "string", "description": "横轴列名"},
                    "y_field": {"type": "string", "description": "纵轴列名"},
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

# ================================================================== 提示词（新增自检规则）
SYSTEM_PROMPT = """你是数据分析助手，可以调用工具查询只读数据库。

【执行规则】
1. 写 SQL 之前先调用 get_schema 了解真实表结构，不要凭空猜字段名。
2. 所有数值必须来自 run_sql 或 plot_chart 的返回结果，禁止自己心算或估计。
3. 用户要求画图时，必须调用 plot_chart。禁止用字符拼图，禁止只输出代码让用户自己运行。
4. 用户要求删除或修改数据时，明确拒绝并说明系统只读；不要提供可直接执行的破坏性 SQL。

【自检规则（给结论之前必须逐条检查）】
5. 引用任何数字时，必须写出它来自哪一列（例如"columns 中的 TotalCustomers = 412"）。
   如果某列的含义不确定，先用 run_sql 查样本数据确认，再下结论。
6. 涉及"有多少个 X"这类计数结论时，必须直接对 X 所在的表执行 COUNT(*) 查询确认，
   禁止从关联查询返回的行数去推断数量（这是曾经出过错的地方）。
7. 如果结果存在并列、歧义、数据缺失，或数据本身是示例数据集，必须主动说明。

【回答风格】
8. 用中文；先给结论，再给依据及其来源列名。"""


# ================================================================== 上下文裁剪
def _role(msg) -> str | None:
    return msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)


def trim_messages(messages: list, max_turns: int = MAX_HISTORY_TURNS) -> list:
    """只保留 system + 最近 N 轮对话。

    ⚠️ 关键：必须保证 assistant(tool_calls) 和对应的 tool 消息成对保留，
       否则 API 会报 "tool message without a preceding tool_calls"。
       这里以 user 消息为"轮次边界"整轮保留，天然保证配对完整。
    """
    if len(messages) <= 1:
        return messages
    system, rest = messages[0], messages[1:]

    turns: list[list] = []
    current: list = []
    for msg in rest:
        if _role(msg) == "user" and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)

    kept = [m for turn in turns[-max_turns:] for m in turn]
    return [system] + kept


# ================================================================== Agent 循环
def solve(question: str, messages: list, seen_errors: set) -> tuple[str, int]:
    messages.append({"role": "user", "content": question})
    total_tokens = 0

    for step in range(1, MAX_STEPS + 1):
        # 每次调用前裁剪一次，控制上下文规模
        trimmed = trim_messages(messages)
        reply = client.chat.completions.create(
            model=MODEL, messages=trimmed, tools=TOOLS
        )
        used = getattr(reply, "usage", None)
        if used:
            total_tokens = used.total_tokens          # 最后一步的用量=本轮最终上下文大小

        msg = reply.choices[0].message
        # 转成 dict：便于裁剪与角色判断，且只保留 API 需要的字段
        entry: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [c.model_dump() for c in msg.tool_calls]
        messages.append(entry)

        if not msg.tool_calls:
            return (msg.content or "(模型没有返回内容)"), total_tokens

        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            print(f"[第{step}步] {name}  {json.dumps(args, ensure_ascii=False)[:130]}")

            result = FUNCS[name](**args) if name in FUNCS else f"没有名为 {name} 的工具"
            text = str(result)

            if "出错" in text or "拒绝执行" in text or "失败" in text:
                key = (name, text[:120])
                if key in seen_errors:
                    text += "\n（提示：你刚刚犯过完全相同的错误，请换一种写法重试。）"
                seen_errors.add(key)

            print(f"         返回：{text[:170]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    return "达到步数上限，未能给出最终答案。", total_tokens


def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("未找到 DEEPSEEK_API_KEY，请先配置 .env 或环境变量。")
        return

    print(f"Chinook 数据分析 Agent v3（clear 清空 / trim 看裁剪 / exit 退出）")
    print(f"配置：保留最近 {MAX_HISTORY_TURNS} 轮 · 单次最多回灌 {MAX_ROWS} 行\n")

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
        low = question.lower()
        if low in {"exit", "quit", "q"} or question == "退出":
            print("已退出。")
            return
        if low == "clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            seen_errors.clear()
            print("记忆已清空。\n")
            continue
        if low == "trim":
            print(f"当前消息数 {len(messages)} → 裁剪后 {len(trim_messages(messages))}\n")
            continue

        try:
            answer, tokens = solve(question, messages, seen_errors)
            print("\n最终回答：", answer)
            print(f"\n[本轮上下文 token：{tokens}]\n")
        except Exception as exc:
            print(f"[错误] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
