"""M5：Streamlit 界面（v4 · 接入 RAG 后同步升级）

v3 → v4 的改动：
    · Agent 内核从 m3_agent 换成 m4_agent（多了一个 search_docs 工具）
    · 示例问题新增一条"口径类"问题，用于演示 RAG 检索
    · 侧边栏标注工具数量与数据字典状态

其余（口令门禁、滚动修复、图表固定宽度）保持不变。

运行：
    streamlit run app.py
通过隧道暴露（另开终端）：
    cloudflared tunnel --url http://localhost:8501
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import docs_rag                                  # noqa: E402
from m4_agent import (                           # noqa: E402
    DB_PATH,
    MAX_HISTORY_TURNS,
    MODEL,
    OUT_DIR,
    SYSTEM_PROMPT,
    TOOLS,
    solve,
)

st.set_page_config(page_title="数据分析 Agent", page_icon="📊", layout="wide")

CHAT_HEIGHT = 560
RENDER_LIMIT = 15
CHART_WIDTH = 780

st.markdown(
    """
    <style>
      html, body, [data-testid="stAppViewContainer"], section.main {
        scroll-behavior: auto !important;
      }
      div[data-testid="stVerticalBlockBorderWrapper"] {
        scroll-behavior: auto !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

EXAMPLES = [
    "消费最高的五位客户是谁？",
    "那只看 2022 年呢？",
    "销售额是怎么定义的？",
    "按月统计销售额趋势，画一个折线图",
    "这些客户一共多少位？",
]


# ------------------------------------------------------------------ 演示口令门禁
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "").strip()

if DEMO_PASSWORD and not st.session_state.get("_authed"):
    st.title("🔒 数据分析 Agent（演示模式）")
    st.caption("这是作者的本地演示实例，需要口令才能使用——避免 API 额度被陌生人消耗。")
    entered = st.text_input("请输入演示口令", type="password")
    if entered:
        if entered == DEMO_PASSWORD:
            st.session_state._authed = True
            st.rerun()
        else:
            st.error("口令不正确")
    st.stop()


# ------------------------------------------------------------------ 会话状态
if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    st.session_state.seen_errors = set()
    st.session_state.history = []
    st.session_state.total_tokens = 0

if "docs_ready" not in st.session_state:
    try:
        docs_rag.ensure_index(verbose=False)
        st.session_state.docs_ready = True
    except Exception:
        st.session_state.docs_ready = False


def _queue(question: str) -> None:
    st.session_state.pending = question


def _reset() -> None:
    st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    st.session_state.seen_errors = set()
    st.session_state.history = []
    st.session_state.total_tokens = 0


def _show_chart(path: str) -> None:
    try:
        st.image(path, caption=Path(path).name, width=CHART_WIDTH)
    except TypeError:
        st.image(path, caption=Path(path).name)


def render_item(item: dict) -> None:
    with st.chat_message("user"):
        st.markdown(item["q"])
    with st.chat_message("assistant"):
        st.markdown(item["a"])
        for chart in item["charts"]:
            _show_chart(chart)
        if item["trace"]:
            with st.expander("🔍 执行过程（工具调用与 SQL）"):
                st.code(item["trace"], language="text")
        st.caption(f"本轮上下文 token：{item['tokens']}")


def process(question: str) -> None:
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("正在检索口径、查看表结构、查询数据库…"):
            before = set(OUT_DIR.glob("*.png")) if OUT_DIR.exists() else set()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    answer, tokens = solve(
                        question, st.session_state.messages, st.session_state.seen_errors
                    )
                after = set(OUT_DIR.glob("*.png")) if OUT_DIR.exists() else set()
                charts = [str(p) for p in sorted(after - before)]
            except Exception as exc:
                answer = f"执行出错：{type(exc).__name__}: {exc}"
                charts, tokens = [], 0

        st.markdown(answer)
        for chart in charts:
            _show_chart(chart)

        trace = buf.getvalue().strip()
        if trace:
            with st.expander("🔍 执行过程（工具调用与 SQL）"):
                st.code(trace, language="text")
        st.caption(f"本轮上下文 token：{tokens}")

    st.session_state.total_tokens += tokens
    st.session_state.history.append(
        {"q": question, "a": answer, "charts": charts, "trace": trace, "tokens": tokens}
    )


# ------------------------------------------------------------------ 侧边栏
with st.sidebar:
    st.title("📊 数据分析 Agent")
    st.caption("用中文提问 → 查口径 → 看表结构 → 写 SQL → 查询 → 画图 → 给结论")

    st.markdown("**运行配置**")
    st.markdown(
        f"- 模型：`{MODEL}`\n"
        f"- 数据库：`{Path(DB_PATH).name}`\n"
        f"- 工具：**{len(TOOLS)} 个**（含数据字典检索）\n"
        f"- 记忆：保留最近 **{MAX_HISTORY_TURNS}** 轮\n"
        f"- 安全：只读连接 + SELECT 白名单 + 关键字拦截"
        + ("\n- 访问：**已开启演示口令**" if DEMO_PASSWORD else "")
    )
    st.caption(
        "数据字典索引：已就绪 ✅" if st.session_state.docs_ready else "数据字典索引：未就绪 ⚠️"
    )

    st.divider()
    st.markdown("**示例问题**（点一下直接问）")
    for q in EXAMPLES:
        st.button(q, on_click=_queue, args=(q,), key=f"ex_{q}")

    st.divider()
    st.button("🧹 清空对话", on_click=_reset)
    st.caption(f"本次会话累计 token：**{st.session_state.total_tokens}**")
    st.caption(f"Streamlit {st.__version__}　|　评测：`python eval/run_eval.py`")

# ------------------------------------------------------------------ 主区域
st.title("数据分析 Agent · Chinook")
st.caption(
    "Agent 能检索数据字典、查看表结构与查询结果，但**没有任何写权限**——全部操作都是只读的。"
)

pending = st.session_state.pop("pending", None)
if pending:
    process(pending)

with st.container(height=CHAT_HEIGHT):
    shown = st.session_state.history[-RENDER_LIMIT:]
    if len(st.session_state.history) > RENDER_LIMIT:
        st.caption(f"（仅显示最近 {RENDER_LIMIT} 条）")
    if not shown:
        st.info(
            "在下面输入问题开始，或点左侧的示例问题。\n\n"
            "试试这两句，能看出区别：\n"
            "· 「消费最高的五位客户是谁？」（数据查询）\n"
            "· 「销售额是怎么定义的？」（RAG 检索数据字典）"
        )
    for item in shown:
        render_item(item)

typed = st.chat_input("用中文提问，例如：消费最高的五位客户是谁？")
if typed:
    process(typed)
    st.rerun()
