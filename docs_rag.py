"""数据字典检索（RAG）——把 RAG 的检索能力复用为 Agent 的一个工具

这是"项目 A（RAG）→ 项目 B（Agent）"的桥梁：
    项目 A 解决"查文档"，项目 B 解决"查数据"，本模块让 Agent 在写 SQL 前先查业务口径。

实现说明（复用项目 A 的经验）：
    · 向量模型：BAAI/bge-small-zh-v1.5（91MB，CPU 可跑，与项目 A 相同）
    · 向量库：Chroma 本地嵌入式，索引目录独立（docs_chroma/），不与项目 A 混用
    · HF 镜像 + 缓存检测离线加载（避免启动时联网校验卡住）

检索范围：只索引 knowledge/ 目录下的文档（业务口径类），项目文档不参与检索。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOWLEDGE_DIR = ROOT / "knowledge"
INDEX_DIR = ROOT / "docs_chroma"
COLLECTION = "data_dictionary"
EMBED_MODEL = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-small-zh-v1.5")

# 必须在 import huggingface_hub 之前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _model_cached(model: str) -> bool:
    """模型权重是否已在本地缓存（缓存完好时直接离线加载，启动更快）。"""
    repo = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{model.replace('/', '--')}"
    return any(repo.rglob("*.safetensors")) if repo.exists() else False


if _model_cached(EMBED_MODEL):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from langchain_chroma import Chroma                      # noqa: E402
from langchain_community.document_loaders import TextLoader  # noqa: E402
from langchain_huggingface import HuggingFaceEmbeddings  # noqa: E402
from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: E402

_EMBEDDINGS = None


def get_embeddings() -> HuggingFaceEmbeddings:
    """构造向量模型（只创建一次，避免重复加载 91MB 权重）。"""
    global _EMBEDDINGS
    if _EMBEDDINGS is None:
        _EMBEDDINGS = HuggingFaceEmbeddings(
            model_name=EMBED_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},   # 归一化后可直用余弦相似度
        )
    return _EMBEDDINGS


def _index_ready() -> bool:
    return (INDEX_DIR / "chroma.sqlite3").exists()


def build_index(verbose: bool = True) -> int:
    """重建索引：读取 knowledge/*.md → 切分 → 向量化 → 写入 docs_chroma/。"""
    files = sorted(KNOWLEDGE_DIR.glob("*.md")) if KNOWLEDGE_DIR.exists() else []
    if not files:
        raise FileNotFoundError(f"知识库目录为空或不存在：{KNOWLEDGE_DIR}")

    docs = []
    for file in files:
        loaded = TextLoader(str(file), encoding="utf-8").load()
        for doc in loaded:
            doc.metadata.setdefault("source", str(file))
        docs.extend(loaded)

    chunks = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=60).split_documents(docs)
    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_id"] = i

    store = Chroma(
        collection_name=COLLECTION,
        embedding_function=get_embeddings(),
        persist_directory=str(INDEX_DIR),
    )
    store.add_documents(chunks)
    if verbose:
        print(f"[docs_rag] 索引 {len(files)} 个文件 -> {len(chunks)} 个片段 -> {INDEX_DIR}")
    return len(chunks)


def ensure_index(verbose: bool = True) -> None:
    """索引不存在时自动构建一次。"""
    if not _index_ready():
        build_index(verbose=verbose)


def _load() -> Chroma:
    return Chroma(
        collection_name=COLLECTION,
        embedding_function=get_embeddings(),
        persist_directory=str(INDEX_DIR),
    )


def search_docs(query: str, k: int = 3) -> str:
    """从数据字典中检索与问题相关的口径定义（Agent 的工具）。

    Args:
        query: 检索问题，例如 "GMV 怎么算" / "活跃客户的定义"
        k: 返回片段数量
    """
    ensure_index(verbose=False)
    try:
        hits = _load().similarity_search_with_score(query, k=k)
    except Exception as exc:
        return f"检索数据字典失败：{exc}"

    if not hits:
        return "数据字典中没有找到相关内容。请基于表结构自行判断，并在回答中说明所采用的口径。"

    parts: list[str] = []
    for i, (doc, score) in enumerate(hits, 1):
        source = Path(doc.metadata.get("source", "未知")).name
        similarity = 1 - float(score)          # Chroma 返回距离，转成相似度便于阅读
        parts.append(
            f"[{i}] 来源：{source}（相似度 {similarity:.3f}）\n{doc.page_content.strip()}"
        )
    return "\n\n".join(parts)


if __name__ == "__main__":
    # 直接运行本文件可以做一次检索自测：python docs_rag.py
    print(build_index())
    for q in ["销售额怎么定义", "客户数和发票数的区别", "活跃客户定义"]:
        print("\n=== 查询：", q, "===")
        print(search_docs(q, k=2))
