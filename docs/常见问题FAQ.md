# 常见问题 FAQ（报错 → 原因 → 解决）

> 全部是**本项目开发过程中真实遇到过**的问题，按"先看报错关键词"检索。

---

## 一、环境与依赖

### 1. `ModuleNotFoundError: No module named 'openai'`（或 `dotenv` / `langchain` / `chromadb`）

**原因**：99% 是**用错了 Python 环境**——依赖装在 conda 环境里，但终端跑的是系统 Python。
不是"包没装"。

**排查**：

```powershell
# ① 看提示符前缀有没有 (data_agent)，没有就激活
conda activate data_agent

# ② 直接问 Python 它是谁
python -c "import sys; print(sys.executable)"
```

输出应该指向 `...\envs\data_agent\python.exe`。

**解决**：

```powershell
conda activate data_agent
python -m pip install -r requirements.txt
```

> 💡 记住：**新开终端窗口会回到 `(base)`**，每次都要重新激活。

### 2. `can't open file 'C:\WINDOWS\system32\main.py'`

**原因**：当前工作目录不对（管理员终端默认落在 `system32`）。

**解决**：

```powershell
cd D:\data_agent
python m4_agent.py
```

> 看提示符里的路径就知道自己在哪：`PS D:\data_agent>` 才是对的位置。

### 3. `ModuleNotFoundError: No module named 'm4_agent'`

**原因**：没有在项目根目录运行，或脚本里的 `sys.path` 没加项目根目录。

**解决**：`cd D:\data_agent` 后再运行；`eval/run_eval.py` 内部已用
`sys.path.insert(0, ROOT)` 处理，无需手动设置。

### 4. 终端里中文乱码

**原因**：Windows 控制台默认 GBK 编码。

**解决**（任选）：

```powershell
# 临时切换当前窗口为 UTF-8
chcp 65001
```

或在脚本开头设置 `PYTHONIOENCODING=utf-8`。项目代码里已对输出做 UTF-8 处理，
若仍乱码，多为**终端字体或编码设置**问题，不影响功能。

---

## 二、模型与检索

### 5. 首次运行卡在下载模型 / 下载超时

**原因**：直连 `huggingface.co` 在国内不稳定。

**解决**：代码已默认设置 `HF_ENDPOINT=https://hf-mirror.com`。
若仍失败，手动设置环境变量后重试：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
[Environment]::SetEnvironmentVariable("HF_ENDPOINT", "https://hf-mirror.com", "User")
```

### 6. 模型明明已缓存，启动时仍要联网校验、断网就卡几分钟

**原因**：`huggingface_hub` 每次启动都会检查文件更新，网络不通时反复重试。

**解决**：`docs_rag.py` 已内置"缓存检测 → 自动离线加载"逻辑。
也可手动强制离线：

```powershell
$env:HF_HUB_OFFLINE = "1"
```

> ⚠️ 必须在 `import huggingface_hub` **之前**生效。

### 7. `FileNotFoundError: 知识库目录为空或不存在`

**原因**：`knowledge/` 目录或其中的 `.md` 文件缺失。

**解决**：确认 `knowledge/数据字典.md` 存在，然后重建索引：

```powershell
python -c "import docs_rag; print(docs_rag.build_index())"
```

### 8. 检索结果不相关 / 相似度很低

**原因**：查询词与文档用词差异过大；或索引未包含最新修改的文档。

**解决**：

```powershell
python -c "import docs_rag; docs_rag.build_index()"   # 重建索引
```

> ℹ️ 实测：`bge-small-zh` 对短查询的**绝对相似度普遍偏低**（0.3–0.5 属正常）。
> 不要凭直觉设"低于 0.7 就不采信"的阈值，必须用真实数据校准。

### 9. `Chroma` 启动很慢 / 想去掉匿名遥测

代码已内置 `ANONYMIZED_TELEMETRY=False`，无需处理。

---

## 三、数据库

### 10. `sqlite3.OperationalError: unable to open database file`

**原因**：`config` 里的数据库路径不对（换电脑、移动目录后最常见）。

**解决**：修改 `m4_agent.py` 顶部的 `DB_PATH`：

```python
DB_PATH = r"D:\data_agent\data\chinook.db"
```

### 11. 模型答错：把"412 张发票"说成"412 位客户"

**原因**：早期版本工具只返回数字、不返回列名，模型只能猜哪一列是什么。

**解决**：已修复——`run_sql` 现在返回结构化结果
（`columns` / `row_count` / `rows` / `truncated`），并在提示词中要求
"计数结论必须直接对目标表 `COUNT(*)`"。

### 12. 模型把"50 行"当成全部数据（数据范围判断错误）

**原因**：`run_sql` 会自动加 `LIMIT` 防止拉整表，但**没有告知模型被截断**。

**解决**：已修复——返回值加入 `row_count`（真实总行数）与 `truncated`（是否截断）标记。

---

## 四、界面与部署

### 13. 页面向上滚轮时被"拽回"下方

**原因**：整页滚动 + Streamlit 自动滚到底（带平滑动画）+ 图片加载导致布局回流，三者互相抢。

**解决**：已改为**固定高度的独立滚动容器** + 关闭平滑滚动 + 图表固定宽度。

### 14. 通过隧道访问时页面一直转圈、组件无响应

**原因**：Streamlit 默认开启 CORS / XSRF 保护，会拦掉来自陌生域名的 WebSocket 连接。

**解决**：仓库已提供 `.streamlit/config.toml`：

```toml
[server]
enableCORS = false
enableXsrfProtection = false
```

> ⚠️ 关闭它们降低了防护等级，**必须配合 `.env` 中的 `DEMO_PASSWORD` 使用**，
> 且演示结束及时关闭隧道。

### 15. `cloudflared : 无法将"cloudflared"项识别为 cmdlet...`

**原因**：装好了，但安装目录不在 PATH 中。

**解决**（任选其一）：

```powershell
# ① 用完整路径调用（路径含空格时必须加 &）
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8501

# ② 临时加入当前会话 PATH
$env:Path += ";C:\Program Files (x86)\cloudflared"

# ③ 永久写入用户 PATH（需重开终端）
[Environment]::SetEnvironmentVariable("Path",
  [Environment]::GetEnvironmentVariable("Path","User") + ";C:\Program Files (x86)\cloudflared", "User")
```

### 16. 隧道进程"打印一堆日志后卡住"

**正常**：它是常驻进程，靠心跳日志维持连接。**不要关掉它**，Ctrl+C 才是有意停止。

---

## 五、评测

### 17. 评测跑完有失败用例，怎么定位？

看 **`eval/last_run_trace.md`**——里面记录了每个用例的提问、工具调用轨迹和完整回答。
再对照 `eval/questions.md` 的"标准答案"逐项核对。

### 18. 自动判定通过，但答案质量感觉很一般

**这是设计上的已知局限**：自动判定只能核对**关键事实**（数字、文件、工具调用），
判断不了"解释是否合理"。所以脚本会另存完整轨迹供**人工抽查**——
**自动通过 ≠ 质量达标**。

---

## 六、Git 与 GitHub

### 19. `.env` 被误提交了怎么办？

```powershell
# ① 停止跟踪（本地文件保留）
git rm --cached .env

# ② 从全部历史中彻底移除
git filter-branch --force --index-filter 'git rm --cached --ignore-unmatch .env' --prune-empty -- --all

# ③ 清理备份引用与悬空对象
git for-each-ref --format='%(refname)' refs/original/ | ForEach-Object { git update-ref -d $_ }
git reflog expire --expire=now --all
git gc --prune=now

# ④ 验证（应无输出）
git log --all --oneline -- .env
```

> 🔐 若已经推送过，**立刻去控制台作废并重新生成 Key**——历史一旦公开，重写也追不回来。

### 20. `git push` 提示 `rejected` / 非快进

**原因**：远程仓库不是空的（建仓库时勾了 README / .gitignore）。

**解决**：建仓库时**什么都不要勾**；或先 `git pull --rebase origin main` 再推。
