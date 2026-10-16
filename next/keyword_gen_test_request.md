# 测试请求：关键字双向生成（keyword_gen）离线对比

日期：2026-10-16。本 PR 只新增离线原型 `keyword_gen.py`、测试 `test_keyword_gen.py` 与本文件，**不修改任何现有程序**，不授权上线、改生产库、连接 NapCat 或发送 QQ 消息。

## 要解决的问题

现有 generate 以输入**末字**为走图起点（`qbot.py` 的 `_generate_path`），输出等于“替对方把这句话说完”，而不是回应这句话。例：输入“想去看电影”，基线回复“院人好难的。”——即“电影→院”的续写。

原型改用 MegaHAL 式方法（[How MegaHAL Works](https://megahal.sourceforge.net/How.html)；[Hutchens & Alder 1998](https://aclanthology.org/W98-1233/)）：

1. 从输入中选信息量高（`-log₂P`）的 1–2 字关键字，排除标点、数字、emoji、`intelligence.STOP_CHARS` 与模型最高频的字母。
2. 从关键字**向后走到消息结尾、向前走到消息开头**。反向概率 `P(前|当前) = w(前,当前)/Σw(·,当前)` 直接来自现有 `chars`，无需新表；句首/句尾停止率从整句数据（`intel_memory` 或导出消息块）自洽估计，缺数据时退回标点+几何长度启发式。
3. 每条输入生成 64 个候选，丢弃禁对、结构异常（复用 `generation_refine.structural_issue`）和复述输入的候选，按关键字惊讶度选最高者。无关键字或无合格候选时不回复，**不退回续写**。

开发机上只用合成语料验证了机制；回复质量**完全未知**，这正是本次测试要回答的问题。

## 边界

- 在 `D:\Projects\QBot` 合并本 PR 后运行。所有输出只写 `runtime\keyword_gen_work*`（已被 `.gitignore` 排除）。Python 使用 `-B -X utf8`。
- 原型对数据库只做只读打开 + SQLite backup 快照，在快照上运行；运行前后不需要停服务。仍请勿在 `/train` 等长时间写入期间运行。
- 不修改线上代码、配置、`data\*.sqlite3`、NapCat；不启停服务；不发群消息。
- **禁止把聊天原文上传 GitHub**：`compare.tsv`、`blind_pairs.tsv`、`answer_key.json`、快照库只留本地。只上传下文“回传内容”列出的汇总文件。

## 步骤

### 1. 单元测试

```powershell
cd D:\Projects\QBot
python -B -X utf8 -m unittest -v
foreach ($s in 0,1,2,3) { $env:PYTHONHASHSEED=$s; python -B -X utf8 -m unittest test_keyword_gen }
Remove-Item Env:PYTHONHASHSEED
```

记录总数与失败项。开发机（Linux, Python 3.14）上已知仅 `test_gate_report_pdf`、`test_gate_review` 的 PDF 相关 2 项因缺少 `vendor\pdf-env` 报错；若测试机还有其他失败，先停下报告，不继续第 2 步。

### 2. 留出对比（主结果，优先）

用一份**时间较新、与群聊同类**的 QQ 文本导出（`pre\text\…`），前 90% 训练临时模型，后 10% 作为没见过的输入：

```powershell
python -B -X utf8 keyword_gen.py compare --export pre\text\<文件夹>\<文件>.txt --holdout 0.1 --prompts 200 --exclude <机器人QQ号> --out runtime\keyword_gen_work_export
```

基线与原型共享同一临时模型、同一输入和种子。若没有合适导出，跳过本步并在报告中说明。

### 3. 生产快照对比（辅助）

对正在使用 generate 的群各跑一次，`--blend` 与线上该群配置一致（按 `runtime\connection.json`，未配置为 0）：

```powershell
python -B -X utf8 keyword_gen.py compare --db data\production.sqlite3 --group 1050045834 --blend 0 --out runtime\keyword_gen_work_1050045834
python -B -X utf8 keyword_gen.py compare --db data\third.sqlite3 --group 1006808801 --blend 0.2 --out runtime\keyword_gen_work_1006808801
```

此模式的输入取自 `intel_memory`，是**已训练过**的句子（`metrics.json` 中 `held_out=false`），复制率会偏高，只作辅助。

### 4. 真人盲评

在第 2 步（没有则第 3 步正式群）的 `blind_pairs.tsv` 中，由了解群聊的真人逐行在最后一列填 `left` / `right` / `same` / `both_bad`。主问题：**“作为群里对上一句的回应，哪条更自然？”** 两条都差就填 `both_bad`，不要替模型改句子。至少评 60 行；不能用 AI 评分代替。然后：

```powershell
python -B -X utf8 keyword_gen.py tally --out runtime\keyword_gen_work_export
```

### 5. 可选：参数敏感度

仅当第 2 步原型 `no_reply_rate` 高于 0.6 时，再跑一次 `--heuristic-bounds`、一次 `--attempts 128`，各自用新的 `--out` 目录。不要扩展成网格。

## 回传内容

测试完成后**直接把结果推送到 GitHub**（`ArabidopsisDev/QBot` 的 `keyword-gen-results` 分支，不需要另开 PR；分支不存在就从合并本 PR 后的 `main` 新建）。只提交以下文件到 `results/keyword_gen/<运行名>/`（运行名如 `export`、`1050045834`）：

- `metrics.json`（只含汇总统计、哈希与参数，不含原文）
- `tally.json`（如做了第 4 步）
- `notes.md`：测试机 Python 版本、第 1 步测试结果、每次运行的命令、导出的大致时间范围与消息量；以及对失败类型的**描述性**观察（如“常从关键字前接不相关短语”），**不要粘贴原文**。

```powershell
git switch -c keyword-gen-results   # 已存在则 git switch keyword-gen-results
New-Item -ItemType Directory -Force results\keyword_gen\export | Out-Null
Copy-Item runtime\keyword_gen_work_export\metrics.json, runtime\keyword_gen_work_export\tally.json results\keyword_gen\export\
# 其余运行同理；再写 results\keyword_gen\notes.md
git add results\keyword_gen
git status --short   # 只应出现 results\keyword_gen 下的 .json / .md
git commit -m "keyword_gen test results"
git push -u origin keyword-gen-results
```

提交前务必确认没有 `.tsv`、`answer_key.json`、`.sqlite3` 被加入；发现就 `git restore --staged` 移除后再提交。

## 看哪些指标

| 指标 | 含义 | 期望 |
|---|---|---|
| `continuation_edge_rate` | 回复首字是否为输入末字的已学后继；基线按构造为 1.0 | 原型明显更低 |
| `keyword_overlap_rate` | 回复含输入话题字 | 原型更高 |
| `no_reply_rate` | 无回复比例 | 记录即可；过高说明关键字或边界太严 |
| `whole_message_copy_rate`、`longest_copy_ratio_mean` | 照搬训练旧句的程度 | 不应显著高于基线 |
| `distinct_reply_rate` | 回复多样性 | 不应明显下降 |
| `latency_ms_p95` | 单条耗时 | 应在百毫秒量级以内 |
| `tally.json` 的 `keyword_net_win_rate` | 真人净胜率 | 决定是否继续的主要依据 |

自动指标只解释机制，不能单独证明“更像人话”。
