# 数据分析 Agent（Text2SQL）

> 用中文提问 → Agent 自动查看表结构 → 生成 SQL → 查询 → 画图 → 用中文给出结论。
> 基于 **DeepSeek function calling** 手写 Agent 循环（不依赖 Agent 框架），配套 Chinook SQLite 数据库。

---

## 当前状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | 最小 Agent：`get_schema` + `run_sql`（三重安全护栏） | ✅ 完成 |
| M2 | `plot_chart` 画图工具 + 多轮记忆 + 提示词约束 + 相同错误检测 + token 计量 | ✅ 完成 |
| M3 | 结果自检、上下文裁剪、字段幻觉治理 | 🚧 进行中 |
| M4 | 评测自动化 | ⬜ 待开始 |
| M5 | Streamlit 界面 + 部署 | ⬜ 待开始 |

**实测数据**：4 类问题通过，2 类问题已定位待修（详见 [`eval/questions.md`](eval/questions.md)）

---

## 功能

| 工具 | 作用 | 安全约束 |
|---|---|---|
| `get_schema` | 返回所有表名 + 字段名，避免模型凭空猜字段 | 只读 |
| `run_sql` | 执行模型生成的 SELECT 查询 | 只读连接 + SELECT 白名单 + 危险关键字拦截 + 自动 LIMIT |
| `plot_chart` | 执行 SQL 并生成折线/柱状/饼图，保存为 PNG | 同上；**模型只传 SQL 和列名，不传数值**，避免数值幻觉 |

---

## 快速开始

```powershell
# 1. 安装依赖
python -m pip install openai matplotlib python-dotenv

# 2. 配置密钥（复制模板后填入自己的 Key）
#    .env 内容：DEEPSEEK_API_KEY=sk-xxxx

# 3. 运行
python m2_agent.py
```

交互命令：`clear` 清空对话记忆 ｜ `exit` 退出

**示例**：

```
你问> 消费最高的五位客户是谁？
[第1步] get_schema  {}
[第2步] run_sql  {"query": "SELECT c.CustomerId, ... ROUND(SUM(i.Total),2) ... GROUP BY c.CustomerId ORDER BY ..."}
最终回答：Helena Holý（捷克，49.62）……
```

---

## 安全设计（三层防御）

1. **只读连接**：`sqlite3.connect("file:...?mode=ro", uri=True)` —— 数据库层面就没有写权限，**这是最硬的一层**
2. **白名单**：只允许 `SELECT` / `WITH` 开头
3. **关键字拦截**：拦截 `INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA/CREATE`

> 设计原则：**安全不能依赖提示词约束模型，必须靠代码层的权限控制兜底。**
> 已实测：要求"删除消费最低的客户"时，模型生成的 DELETE 被拦截。

---

## 关键技术决策

1. **手写 Agent 循环，不用框架** —— 为了搞清工具调用、错误回灌、终止条件的机制
2. **`plot_chart` 只接收 SQL + 列名** —— 让代码去数据库取数，而不是让模型转录数字
3. **错误回灌** —— SQL 报错不抛出，而是把错误原文交回模型让它自己改
4. **相同错误检测** —— 同一错误重复出现时给出提示，避免原地打转

---

## 已知问题（M3 要解决）

| # | 问题 | 证据 |
|---|---|---|
| 1 | **数值误读**：把"412 张发票"说成"412 位客户"（实际客户 59 位），且语气自信 | [观察日志](docs/观察日志.md) 发现 A |
| 2 | **上下文膨胀**：连续 4 轮对话 token 从 1874 涨到 5700 | 观察日志 发现 B |
| 3 | 结果存在并列时虽会说明，但 `LIMIT 5` 的取法仍不稳定 | eval 第 1 题 |
| 4 | 画图请求已验证可生成文件，但尚未校验图表内容是否正确 | eval 第 3 题 |

---

## 项目结构

```
data_agent/
├── m1_minimal_agent.py     # M1：最小 Agent
├── m2_agent.py             # M2：加画图/记忆/约束（当前主程序）
├── data/chinook.db         # Chinook 示例数据库
├── eval/questions.md       # 评测问题集与实测结果 ⭐
├── docs/观察日志.md         # Agent 行为观察记录 ⭐
├── outputs/                # 生成的图表（已 gitignore）
└── .env                    # API Key（已 gitignore）
```

---

## 技术栈

Python · SQLite · DeepSeek API（function calling）· matplotlib

## 声明

`data/chinook.db` 为公开教学示例数据库，不含任何真实业务数据。
