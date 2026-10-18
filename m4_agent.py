"""M4：数据分析 Agent v4 —— 接入 RAG，解决"口径歧义"

本版本的核心新增：把项目 A 的 RAG 检索能力封装成 Agent 的**第 4 个工具** `search_docs`。

    为什么需要它（真实痛点）：
        Text2SQL 最大的坑不是"不会写 SQL"，而是"不懂业务口径"。
        用户问"上个月的销售额是多少"——是 SUM(Invoice.Total)，还是明细行金额求和？
        "客户有多少位"——是 Customer 表行数，还是有过订单的客户数？

    解决方式：
        Agent 在写聚合查询前，先检索【数据字典】（knowledge/数据字典.md）确认口径，
        再生成 SQL。这就是把 RAG 从"独立项目"变成"Agent 的一个可复用能力"。

    架构：
        文档链路：knowledge/*.md → BGE 向量化 → Chroma(docs_chroma/) → search_docs 工具
        数据链路：chinook.db → SQL 白名单 + 只读连接 → run_sql / plot_chart 工具
        两条链路在 Agent 循环里汇合。

运行：
    python m4_agent.py
命令：
    clear 清空记忆 ｜ trim 看裁剪效果 ｜ index 重建数据字典索引 ｜ exit 退出
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt            # noqa: E402

from openai import OpenAI                  # noqa: E402

import docs_rag                            # noqa: E402  （RAG 检索能力）

# ------------------------------------------------------------------ 配置
DB_PATH = r"D:\data_agent\data\chinook.db"
OUT_DIR = Path(r"D:\data_agent\outputs")
MODEL = "deepseek-chat"
MAX_STEPS = 7
MAX_HISTORY_TURNS = 3
MAX_ROWS = 20
MAX_CELL = 200

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


# ================================================================== 数据侧工具
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
    text = str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


# ------------------------------------------------------------------ 终端显示辅助
_MD_RULES = (
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),        # 标题的 #
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),               # **加粗**
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),   # *斜体*
    (re.compile(r"`([^`]*)`"), r"\1"),                   # `行内代码`
    (re.compile(r"^\s*[-*+]\s+", re.M), "  · "),         # 列表符号
    (re.compile(r"^\s*\|.*\|\s*$", re.M), ""),           # 表格行（终端不渲染，直接去掉）
)


def strip_markdown(text: str) -> str:
    """把 Markdown 文本转成适合终端显示的形式。

    只用于【终端展示】，不改变回灌给模型的内容——
    数据字典原文保持 Markdown（仓库里给人读，结构也有助于切分与模型理解），
    但终端不渲染 Markdown，所以打印前先清理符号。
    """
    out = text
    for pattern, repl in _MD_RULES:
        out = pattern.sub(repl, out)
    return out


def run_sql(query: str) -> str:
    """执行 SELECT，返回带列名的结构化结果（列名 / 总行数 / 是否截断）。"""
    cols, rows, err = _fetch(query)
    if err:
        return err
    payload = {
        "columns": cols,
        "row_count": len(rows),
        "rows": [[_short(v) for v in row] for row in rows[:MAX_ROWS]],
        "truncated": len(rows) > MAX_ROWS,
    }
    return json.dumps(payload, ensure_ascii=False)


def plot_chart(query: str, x_field: str, y_field: str, chart_type: str = "line", title: str = "") -> str:
    """执行 SQL 并画图（模型只传 SQL 和列名，不传数值）。"""
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


FUNCS = {
    "get_schema": get_schema,
    "run_sql": run_sql,
    "plot_chart": plot_chart,
    "search_docs": docs_rag.search_docs,      # ⭐ 来自 RAG 模块
}

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
            "name": "search_docs",
            "description": (
                "检索【数据字典】，查询指标口径与业务定义（例如'销售额''客户数''活跃客户'怎么算）。"
                "遇到口径不确定、或用户使用业务术语时，先调用它再写 SQL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查的口径或术语，例如 '销售额口径'"},
                    "k": {"type": "integer", "description": "返回片段数，默认 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "执行 SELECT 查询。返回 columns（列名）、row_count（总行数）、rows（数据行）、truncated（是否截断）。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "一条 SQLite SELECT 语句"}},
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
                    "chart_type": {"type": "string", "enum": ["line", "bar", "pie"], "description": "默认 line"},
                    "title": {"type": "string", "description": "图表标题"},
                },
                "required": ["query", "x_field", "y_field"],
            },
        },
    },
]

# ================================================================== 提示词
# 拆成「基础规则」+「输出格式」两部分：CLI 与 Web 的渲染能力不同，
# 输出格式应当由【渲染端】决定，而不是让模型去猜。
#   · 终端：不支持 Markdown 渲染 → 纯文本
#   · Streamlit：支持 Markdown → 表格 / 加粗
BASE_PROMPT = """你是数据分析助手，可以调用工具查询只读数据库，也可以检索数据字典。

【工具选择规则】
1. 涉及业务术语或指标口径（如"销售额""客户数""活跃客户""客单价"）时，
   先调用 search_docs 查数据字典确认定义，再写 SQL。不要凭自己的理解猜口径。
2. 写 SQL 之前先调用 get_schema 了解真实表结构。
3. 用户要求画图时，必须调用 plot_chart。禁止用字符拼图，禁止只输出代码让用户运行。
4. 用户要求删除或修改数据时，明确拒绝并说明系统只读；不要提供可直接执行的破坏性 SQL。

【自检规则（给结论前逐条检查）】
5. 引用任何数字时，说明它来自哪一列（例如"CustomerCount = 59"）。
6. 涉及"有多少个 X"这类计数结论时，必须直接对 X 所在的表执行 COUNT(*) 确认，
   禁止从关联查询返回的行数去推断数量。
7. 如果结果存在并列、歧义、数据缺失，或数据本身是示例数据集，必须主动说明。
8. 回答中要说明所采用的口径（来自数据字典的哪一条）。"""

STYLE_HINTS = {
    "markdown": (
        "\n\n【输出格式】\n9. 用中文作答；先给结论，再给依据（含来源列名与口径出处）。"
        "可以使用 Markdown 的标题、表格与加粗来组织答案。"
    ),
    "plain": (
        "\n\n【输出格式】\n9. 用中文作答；先给结论，再给依据（含来源列名与口径出处）。"
        "当前运行在**终端环境，不支持 Markdown 渲染**，所以请输出纯文本：\n"
        "   · 不要使用 ** 加粗、不要使用 | 表格、不要使用 # 标题、不要使用反引号\n"
        "   · 用短横线列表和缩进组织内容；键值对用「名称：值」的形式逐行列出\n"
        "   · 用「结论：」「依据：」这样的文字小标题分段，不要用 ## "
    ),
}


def build_system_prompt(style: str = "markdown") -> str:
    """按渲染环境拼装系统提示词（style: 'markdown' 或 'plain'）。"""
    return BASE_PROMPT + STYLE_HINTS.get(style, STYLE_HINTS["markdown"])


# Streamlit 界面与评测脚本使用这份（Markdown 环境）
SYSTEM_PROMPT = build_system_prompt("markdown")


# ================================================================== 上下文裁剪
def _role(msg) -> str | None:
    return msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)


def trim_messages(messages: list, max_turns: int = MAX_HISTORY_TURNS) -> list:
    """只保留 system + 最近 N 轮（以 user 消息为轮次边界，保证 tool_calls 与 tool 消息成对）。"""
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
    return [system] + [m for turn in turns[-max_turns:] for m in turn]


# ================================================================== Agent 循环
def solve(question: str, messages: list, seen_errors: set) -> tuple[str, int]:
    messages.append({"role": "user", "content": question})
    total_tokens = 0

    for step in range(1, MAX_STEPS + 1):
        trimmed = trim_messages(messages)
        reply = client.chat.completions.create(model=MODEL, messages=trimmed, tools=TOOLS)
        used = getattr(reply, "usage", None)
        if used:
            total_tokens = used.total_tokens

        msg = reply.choices[0].message
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

            print(f"         返回：{strip_markdown(text)[:170]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    return "达到步数上限，未能给出最终答案。", total_tokens


def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("未找到 DEEPSEEK_API_KEY，请先配置 .env 或环境变量。")
        return

    print("正在准备数据字典索引…")
    try:
        docs_rag.ensure_index()
        print(f"数据字典就绪：{docs_rag.INDEX_DIR}")
    except Exception as exc:
        print(f"[警告] 数据字典索引失败，search_docs 将不可用：{exc}")

    print("\nChinook 数据分析 Agent v4（clear / trim / index / exit）")
    print(f"配置：保留最近 {MAX_HISTORY_TURNS} 轮 · 单次最多回灌 {MAX_ROWS} 行 · 工具数 {len(TOOLS)}")
    print("输出格式：纯文本（终端不渲染 Markdown）\n")

    # 终端环境用 plain，界面环境用 markdown
    messages: list = [{"role": "system", "content": build_system_prompt("plain")}]
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
            messages = [{"role": "system", "content": build_system_prompt("plain")}]
            seen_errors.clear()
            print("记忆已清空。\n")
            continue
        if low == "trim":
            print(f"当前消息数 {len(messages)} → 裁剪后 {len(trim_messages(messages))}\n")
            continue
        if low == "index":
            try:
                n = docs_rag.build_index()
                print(f"数据字典索引已重建：{n} 个片段\n")
            except Exception as exc:
                print(f"重建失败：{exc}\n")
            continue

        try:
            answer, tokens = solve(question, messages, seen_errors)
            print("\n最终回答：", answer)
            print(f"\n[本轮上下文 token：{tokens}]\n")
        except Exception as exc:
            print(f"[错误] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
