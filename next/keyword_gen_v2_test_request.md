# 测试请求：keyword_gen 第二轮（变阶上下文 + 自然长度）

日期：2026-10-16。仍是离线原型，只改 `keyword_gen.py` 与 `test_keyword_gen.py`，不接入 `live.py`，不授权上线。边界与第一轮 [keyword_gen_test_request.md](keyword_gen_test_request.md) 相同：只读快照、不写生产库、不连 NapCat、不发消息、**不上传聊天原文**。

## 第一轮结论与本轮改动

第一轮去掉了续写感（续写边率 100% → 5–10%），但 60 条真人盲评 22:21 持平、15 条两条都差（`results/keyword_gen/notes.md`）。判断：起点问题解决后，瓶颈是句内仍按单字拼接，且回复偏长。

本轮两项改动，关键字选择与惊讶度打分不变：

1. **变阶上下文**：前向/反向每一步按最近 1–3 字上下文抽字，Witten-Bell 插值逐级退回第一轮的 `chars` + 句首/句尾分布。上下文须在 ≥2 条**不同消息**中出现才参与（`--min-support`），一人复读不能当证据；候选字只能是 `chars` 已有的边，已删边/禁对不会复活。统计来自 `intel_memory` 整句（导出模式用训练段消息块），与句首/句尾率同源。
2. **自然长度**：关键字常在句中，两侧各按无记忆停止率走，原始候选约为真实消息两倍长，打分又偏好长句。每次回复从同一语料的消息长度分布抽目标长度，候选先按“是否走到真实句首句尾”、再按与目标长度的接近程度（4 字一档）、最后按惊讶度排序。

`--order 1` 等于第一轮原型。`compare` 现同时输出基线、第一轮（`keyword_v1`）、第二轮（`keyword`）三列，以及两份盲评表（对基线 `blind_pairs.tsv`，对第一轮 `blind_pairs_v1.tsv`）。`tally --name v1` 汇总后者。

## 开发机评测（公开语料，AI 盲评）

真实群聊不能离开测试机，开发机改用公开的 [LCCC](https://huggingface.co/datasets/silver/lccc)（微博短对话）随机 133 页：13,300 段对话，按对话随机切分，90% 训练（35,522 条），留出对话中取 150 个首句作输入。按用户要求由 AI 子代理盲评，不看模型身份与答案映射。**AI 评审不等于真人评价**；LCCC 也不是 QQ 群聊。

结果见 `results/keyword_gen_v2/lccc/`（只含统计）：

| 指标 | 基线 | 第一轮 | 第二轮 |
|---|---:|---:|---:|
| 无回复率 | 0.67% | 0% | 0% |
| 回复长度中位数（真人回复为 8） | 27 | 26.5 | **10** |
| 续写边率* | 100% | 62% | 62.7% |
| 最长复制比例均值 | 22.3% | 17.7% | 42.8% |
| 完整旧句复制率 | 0% | 0% | 0% |
| 延迟中位数 / P95（ms） | 13 / 25 | 74 / 92 | 242 / 331 |

\*LCCC 训练集远比各群库稠密，几乎任意两字都是已学边，此指标在这里区分度低（群库上第一轮为 5–10%）。

AI 盲评（各 150 对，全部有效）：

| 对比 | 第二轮胜 | 对手胜 | 差不多 | 都差 | 净胜率 |
|---|---:|---:|---:|---:|---:|
| 第二轮 vs 基线 | 114 | 0 | 7 | 29 | +76% |
| 第二轮 vs 第一轮 | 78 | 9 | 26 | 37 | +46% |

按两条回复长度差分层（评审自述“短句反而能读”，必须拆开看）：

| 对比 | 长度差 ≤5 字的 34 对 | 对手长 5 字以上 |
|---|---|---|
| vs 基线 | 第二轮 25 胜、基线 0 胜、3 同、6 都差 | 83 胜 / 0 负 / 3 同 / 18 都差（104 对） |
| vs 第一轮 | 第二轮 7 胜、第一轮 4 胜、15 同、8 都差 | 70 胜 / 2 负 / 9 同 / 25 都差（106 对） |

解读：

- **对基线的优势不只是长度**：长度相近时仍 25:0。去掉续写入口 + 带入话题字确有效果。
- **对第一轮的优势主要来自长度**：长度相近时只有 7:4，15 对差不多。变阶上下文本身的作用在本数据上很小；真正起作用的是让回复长度接近真人消息。
- 仍有约 20–25% 两条都差。评审描述的共同失败：长句话题漂移、复读输入词或叠字（“当当当当”）、相邻词粘成假词。第二轮变短后复制比例升到 0.43，部分是短句的自然结果，部分是复读输入词，需要在群库上再看。
- 盲评文件第 50–149 行曾被评审脚本截断后由其手工重建；已用相同种子重新生成并逐格比对，150 行前四列与答案映射完全一致。

## 测试机步骤

1. 单元测试，同第一轮：

```powershell
cd D:\Projects\QBot
python -B -X utf8 -m unittest -v
foreach ($s in 0,1,2,3) { $env:PYTHONHASHSEED=$s; python -B -X utf8 -m unittest test_keyword_gen }
Remove-Item Env:PYTHONHASHSEED
```

2. 三个 generate 群各跑一次快照对比（`--blend` 与线上一致）：

```powershell
python -B -X utf8 keyword_gen.py compare --db data\production.sqlite3 --group 1050045834 --blend 0 --out runtime\keyword_gen_v2_1050045834
python -B -X utf8 keyword_gen.py compare --db data\third.sqlite3 --group 1006808801 --blend 0.2 --out runtime\keyword_gen_v2_1006808801
python -B -X utf8 keyword_gen.py compare --db data\fourth.sqlite3 --group 929618496 --blend 0 --out runtime\keyword_gen_v2_929618496
```

不需要真人盲评。

3. 只回传 `metrics.json` 到 `results/keyword_gen_v2/<群号>/`，另写 `results/keyword_gen_v2/notes.md`（Python 版本、单元测试结果、命令、`complete_rate` 与延迟是否正常；**不贴原文**）。直接推送到 `ArabidopsisDev/QBot` 的 `keyword-gen-results` 分支：

```powershell
git switch keyword-gen-results
git merge main        # 带上本 PR 合并后的代码
# 复制各群 metrics.json 到 results\keyword_gen_v2\<群号>\，写 notes.md
git add results\keyword_gen_v2
git status --short    # 只应出现 .json / .md
git commit -m "keyword_gen v2 test results"
git push
```

## 看哪些指标

- `keyword.length_median` 应接近真实消息长度（不再是基线的 15–27 字）。
- `keyword.complete_rate` 接近 1。
- `longest_copy_ratio_mean`：短句天然比例更高；若明显超过 0.5 或 `whole_message_copy_rate` > 0，说明变阶在照搬旧句，需提高 `--min-support`。
- `latency_ms_p95`：开发机 35k 条语料约 330 ms；生产库更小，应更快。超过 1 秒请注明。
