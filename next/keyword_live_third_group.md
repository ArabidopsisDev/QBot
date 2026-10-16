# 上线：第三群 generate 回复 30% 走关键字回复

日期：2026-10-16。按用户决定，在**第三群 1006808801** 开启，其他三个群不变。

## 改动

- 新增 `keyword_reply.py`：关键字双向生成引擎（第二轮原型 + 防照搬），由 `keyword_gen.py` 拆出；`keyword_gen.py` 仅保留离线对比工具。
- `qbot.py`：`Bot(keyword_reply_probability=…, keyword_rng=…)`，默认 0.0；新增 `Bot.keyword_reply`。只在 **generate 模式**、普通消息完成学习之后，以该概率代替 `generate(text[-2:])`。match/intelligence、命令、门禁、质量分抽样、限速、发送路径全部不变。
- `live.py`：`KEYWORD_REPLY_PROBABILITY = {THIRD_GROUP: .30}`；启动日志多一行 `Keyword replies: 1006808801 30% of generate replies.`
- 新增 `test_keyword_live.py`（9 项）；README“模型”一节已记录。

### 安全约束

- 每对相邻字都是 `chars` 已有边；内存模型最多 60 秒重建一次（时钟回拨也会重建），回复返回前再逐对查询**当前** `chars`，后台删除或 `/clear generate` 后立即不会发出已删边。出站禁对仍由 `Bot.handle` 统一检查。
- 防照搬：候选等于任一 `intel_memory` 训练消息，或长度 ≥6 且 ≥80% 来自同一条旧消息的连续片段，一律丢弃（第二轮测试机上曾有 0.53%–1.06% 整句照搬）。
- 没有关键字或合格候选时**不回复**，不退回续写。
- 比例为 0 的群不触碰 `keyword_rng`，原 generate 的随机序列完全不变（有测试）。

### 性能（开发机，5,000 条记忆）

首次/每分钟重建约 0.6 秒，缓存后单条约 76 ms（最大 115 ms）。重建在消息处理线程内同步进行，远低于 HTTP 5 秒超时。

## 评测依据

- 测试机三群快照（第二轮，`results/keyword_gen_v2/`）：长度中位数 8–9 字（各群真实消息 7–9），续写边率 6–12%（原 generate 100%），无回复率 6%（原 33–40%），P95 < 26 ms。
- 开发机 LCCC + AI 盲评：对原 generate 114:0（长度相近的 34 对中 25:0）。AI 评审不等于真人；输入为训练过的句子或微博语料，不是第三群的未见消息。

## 测试机步骤

1. 合并本 PR 后：

```powershell
cd D:\Projects\QBot
python -B -X utf8 -m unittest -v
foreach ($s in 0,1,2,3) { $env:PYTHONHASHSEED=$s; python -B -X utf8 -m unittest test_keyword_live test_keyword_gen }
Remove-Item Env:PYTHONHASHSEED
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-live.ps1 -CheckOnly
```

任何失败先停下报告，不重启。

2. 防照搬复测（可选，确认整句复制率为 0）：

```powershell
python -B -X utf8 keyword_gen.py compare --db data\third.sqlite3 --group 1006808801 --blend 0.2 --out runtime\keyword_live_third
```

3. 重启机器人使改动生效（`stop.cmd` → `start.cmd`），确认启动日志出现 `Keyword replies: 1006808801 30%`，且 `isLogin=true`、`coreReady=true`。重启有 1–2 分钟消息黑洞，建议在群内安静时进行。
4. 推送 `results/keyword_live/notes.md` 到 `keyword-gen-results` 分支：Python 版本、单元测试结果、CheckOnly 结果、重启时间、日志中是否出现上述行；若做了第 2 步，附 `metrics.json`。**不贴群聊原文**。

## 回滚

`live.py` 中改为 `KEYWORD_REPLY_PROBABILITY = {}`（或把 0.30 调小），重启即可。数据库没有任何结构或数据变更，无需迁移或恢复。

## 之后观察

上线后由群友自然使用即可（按用户意见，不安排集中真人评测）。如出现明显复读输入词、照搬旧句或刷屏感，先调低比例再分析。
