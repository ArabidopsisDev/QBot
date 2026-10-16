# keyword_gen 测试结果

测试日期：2026-10-16（Asia/Singapore）。代码基线：b32069e，合并 PR #1 后的 main。
测试机：Windows，Python 3.12.10（64 位）。所有命令在 `D:\Projects\QBot` 执行，Python 均使用 `-B -X utf8`；TEMP/TMP/TMPDIR 指向 `runtime/keyword_gen_work_tests`。

## 单元测试

- `python -B -X utf8 -m unittest -v`：286 项全部通过，17.329 秒；无失败、错误或跳过，包括 PDF 测试。
- 依次设置 `PYTHONHASHSEED=0,1,2,3`，分别运行 `python -B -X utf8 -m unittest test_keyword_gen`：每次 11 项全部通过，共 44 次；运行后移除环境变量。
- 测试日志只保存在本机 `runtime/keyword_gen_work_tests`，不上传日志或合成测试的 tally。

## 留出测试：跳过

`pre/text` 仅有专项词汇/辱骂/歧视数据集和宋词语料，未发现近期、同类群聊 QQ 文本导出；`pre` 的其他位置也没有 `.txt` 导出。现有文件不满足主结果的数据条件，因此不使用它们凑出留出对比，不生成 export 主结果。群聊导出的时间范围及消息量：无合适导出，无法提供。

## 生产只读快照对比

检查各群 settings 与 connection.json 后，对当前启用 generate 的三个群分别运行；测试群当时为 intelligence，不纳入。每组 200 个输入，种子 1，attempts=64，refine=0.30。基线 blend 与线上对应群一致：0、0.2、0。

```powershell
python -B -X utf8 keyword_gen.py compare --db data\production.sqlite3 --group 1050045834 --blend 0 --out runtime\keyword_gen_work_1050045834
python -B -X utf8 keyword_gen.py compare --db data\third.sqlite3 --group 1006808801 --blend 0.2 --out runtime\keyword_gen_work_1006808801
python -B -X utf8 keyword_gen.py compare --db data\fourth.sqlite3 --group 929618496 --blend 0 --out runtime\keyword_gen_work_929618496
```

三个命令并发运行，总共 600 条输入、1200 次模型比较调用。计时包含并发竞争，应视为本机本轮观测值，不是独占基准。三个快照初始 intel_memory 条目数分别为 1138、5000、285，频次权重分别为 1277、6020、298；这不是导出消息总量，也不提供完整训练数据的时间范围。

输入全部来自已训练的 intel_memory，所有 metrics 的 held_out=false。基线与原型使用同一组快照、同一批输入和相同的逐条种子。

| 群 | 模型 | 无回复率 | 续写边率 | 关键词重合率 | 完整旧句复制率 | 最长复制比例均值 | 不同回复比例 | P95 延迟 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1050045834 | baseline | 34.00% | 100.00% | 12.88% | 0.00% | 36.47% | 100.00% | 4.71 |
| 1050045834 | keyword | 6.00% | 5.32% | 100.00% | 0.00% | 24.23% | 100.00% | 7.84 |
| 1006808801 | baseline | 47.00% | 100.00% | 17.92% | 0.94% | 32.05% | 100.00% | 8.53 |
| 1006808801 | keyword | 6.50% | 10.16% | 100.00% | 0.00% | 22.28% | 100.00% | 8.48 |
| 929618496 | baseline | 33.00% | 100.00% | 16.42% | 0.00% | 35.00% | 98.51% | 4.25 |
| 929618496 | keyword | 5.00% | 7.37% | 100.00% | 0.00% | 25.99% | 100.00% | 8.50 |

无回复率分母为全部输入；重合率、复制率、多样性与续写边率等采用实际有回复条目作为分母，详见实现。原型续写边率明显下降、话题字重合率上升；三组没有观察到完整旧句复制率或最长复制比例增加。所有原型 P95 均低于 9 ms。自动指标不能证明句子自然、语义相关或更像真人；没有真人评分前，不作语义失败类型判定或上线建议。

## 真人盲评：待完成

由于没有合适留出集，按请求使用正式群 `runtime/keyword_gen_work_1050045834/blind_pairs.tsv` 作为主盲评文件。200 行的 choice 均为空，尚未进行真人评分。请由了解群聊的真人至少评 60 行，最后一列填写 `left`、`right`、`same` 或 `both_bad`，之后执行：

```powershell
python -B -X utf8 keyword_gen.py tally --out runtime\keyword_gen_work_1050045834
```

此时才能把正式群 tally.json 加到对应结果目录；本次不提交虚构的 tally.json，也不使用 AI 代替真人盲评。真人净胜率和自然度结论当前未知。

## 可选参数敏感度：未触发

需求限定仅在第 2 步原型 no_reply_rate > 0.6 时运行。第 2 步因缺少合适导出被跳过，因此未扩展参数。快照的原型无回复率也只有 5%–6.5%。

## 数据边界

未修改线上源码、连接配置、生产数据库或 NapCat；未启停服务、未发群消息。数据库源使用 mode=ro 打开，通过 SQLite backup 复制到 runtime 后运行；在线服务仍可自行产生正常写入，不能用前后源文件哈希变化判定为本测试写库。

compare.tsv、blind_pairs.tsv、answer_key.json、模型快照及测试日志全部只留在被忽略的 runtime 下。仅提交此 notes.md 与三组 metrics.json，后者只含参数、路径、哈希和汇总统计，不含聊天原文。

## 回传状态

已建立本地 `keyword-gen-results` 分支并提交四个允许回传的汇总文件。直接访问 GitHub 时连接失败；沿用 Windows 系统代理后 API 连通，但 Git 推送因本机缺少可用的 GitHub HTTPS 凭据而失败（无法读取 Username）。未上传聊天原文。结果仍在本地，待在 GitHub Desktop 推送此分支，或完成 Git 登录后执行 `git push -u origin keyword-gen-results`。真人盲评至少 60 行与 tally 汇总也仍待完成。
