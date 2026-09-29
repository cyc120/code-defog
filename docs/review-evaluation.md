# 审查结果评测

Code Defog 使用人工裁定的已知缺陷案例评估审查报告。审查模型不参与打分，也不会自动把待核实线索转换成确认缺陷。

## 当前试点集

`cases/benchmark.json` 收录了两个已有修复前后复现证据的行为缺陷，以及一个分类护栏案例。护栏案例用于检查审查是否把过时测试的失败误说成产品行为缺陷，不计入缺陷召回率。

这是试点集，不代表不同语言、项目规模或缺陷类型下的总体准确率。当前没有经过整理的干净改动对照，因此报告里的“误报比例”只是本批已裁定问题中的比例，不能当作统计意义上的误报率。

## 评测步骤

1. 对某个案例的缺陷版本运行 Code Defog 或 Skill，保存原始 JSON 报告。
2. 根据案例 README 中的复现证据逐项人工裁定报告发现。
3. 把每条报告发现标成 `true_positive`、`false_positive` 或 `unverified`。真阳性需要填入对应的 `case_id`；一个金标准案例只能匹配一次。
4. 为没有被任何报告发现命中的金标准案例记录漏检；评分脚本会自动计算。
5. 单独检查 `classification_guardrails`，确认报告没有把检查脚本故障误报成产品缺陷。

裁定文件格式：

```json
{
  "report_id": "review-id",
  "finding_decisions": [
    {"finding_index": 0, "result": "true_positive", "case_id": "rhinelabui-001", "note": "位置与复现结果吻合"},
    {"finding_index": 1, "result": "false_positive", "note": "证据不能支持该结论"},
    {"finding_index": 2, "result": "unverified", "note": "现有材料不足以裁定"}
  ]
}
```

对无发现的报告，使用空数组 `"finding_decisions": []`。评分前，报告里的每条发现都必须完成裁定。

## 计算

```powershell
python scripts/score_review_eval.py --report path/to/report.json --adjudication path/to/adjudication.json --output path/to/score.json
```

脚本输出金标准集上的召回率、已裁定发现中的精确率/误报比例、待核实数量和裁定覆盖率。`unverified` 单独呈现，不计为已确认真阳性或误报。已确认缺陷仍须依据独立复现证据，不由模型自我声明。

评测报告应同时保存模型/提供方、代码修订、审查范围、案例集版本和人工裁定记录。扩充评测集时，加入不同语言的正例、干净改动对照、误报历史和修复后版本；目前的三个示例不足以支撑跨项目结论。
