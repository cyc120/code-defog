#!/usr/bin/env python3
"""Render a shareable Word review report from a structured review JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as ET

def load_review(path: Path) -> dict:
    """Load the standalone review JSON used by this skill's DOCX reporter."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read review JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("review"), dict):
        raise ValueError("review JSON must contain a review object")
    for key in ("repository", "revision", "scope_summary"):
        if key not in data["review"]:
            raise ValueError(f"review metadata is missing {key!r}")
    if not isinstance(data.get("findings", []), list) or not isinstance(data.get("checks", []), list):
        raise ValueError("findings and checks must be arrays")
    return data


CATEGORY_LABELS = {
    "behavior_defect": "行为缺陷",
    "content_rule_inconsistency": "规则/数据不一致",
    "factual_content_error": "事实性内容错误",
    "regression_check_failure": "回归检查失败",
    "maintenance_gap": "文档维护",
    "unverified_lead": "待核实",
    "environment_issue": "环境限制",
}
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "unrated": 4}
CATEGORY_ORDER = {"behavior_defect": 0, "content_rule_inconsistency": 1, "regression_check_failure": 2, "factual_content_error": 3}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", W)


def tag(name: str) -> str:
    return f"{{{W}}}{name}"


def run(text: str, *, bold: bool = False, color: str | None = None, size: int = 18) -> ET.Element:
    element = ET.Element(tag("r"))
    props = ET.SubElement(element, tag("rPr"))
    ET.SubElement(props, tag("rFonts"), {
        tag("ascii"): "Aptos", tag("hAnsi"): "Aptos", tag("eastAsia"): "Microsoft YaHei"
    })
    if bold:
        ET.SubElement(props, tag("b"))
    if color:
        ET.SubElement(props, tag("color"), {tag("val"): color})
    ET.SubElement(props, tag("sz"), {tag("val"): str(size)})
    node = ET.SubElement(element, tag("t"))
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = text
    return element


def paragraph(text: str = "", *, style: str = "Normal", bold: bool = False,
              color: str | None = None, size: int = 18, keep_next: bool = False) -> ET.Element:
    element = ET.Element(tag("p"))
    props = ET.SubElement(element, tag("pPr"))
    ET.SubElement(props, tag("pStyle"), {tag("val"): style})
    if keep_next:
        ET.SubElement(props, tag("keepNext"))
    if text:
        element.append(run(text, bold=bold, color=color, size=size))
    return element


def cell(lines: list[tuple[str, bool]], width: int, *, header: bool = False,
         shade: str | None = None) -> ET.Element:
    element = ET.Element(tag("tc"))
    props = ET.SubElement(element, tag("tcPr"))
    ET.SubElement(props, tag("tcW"), {tag("w"): str(width), tag("type"): "dxa"})
    if header:
        shade = "17365D"
    if shade:
        ET.SubElement(props, tag("shd"), {tag("fill"): shade})
    ET.SubElement(props, tag("vAlign"), {tag("val"): "center"})
    for text, emphasize in lines or [("", False)]:
        p = paragraph(text, style="TableText", bold=emphasize or header,
                      color="FFFFFF" if header else "203247", size=17)
        element.append(p)
    return element


def table(headers: list[str], rows: list[list[list[tuple[str, bool]]]], widths: list[int]) -> ET.Element:
    element = ET.Element(tag("tbl"))
    props = ET.SubElement(element, tag("tblPr"))
    ET.SubElement(props, tag("tblW"), {tag("w"): str(sum(widths)), tag("type"): "dxa"})
    ET.SubElement(props, tag("tblLayout"), {tag("type"): "fixed"})
    borders = ET.SubElement(props, tag("tblBorders"))
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        ET.SubElement(borders, tag(edge), {tag("val"): "single", tag("sz"): "5", tag("color"): "CBD5E1"})
    margins = ET.SubElement(props, tag("tblCellMar"))
    for edge, amount in (("top", 90), ("left", 100), ("bottom", 90), ("right", 100)):
        ET.SubElement(margins, tag(edge), {tag("w"): str(amount), tag("type"): "dxa"})
    grid = ET.SubElement(element, tag("tblGrid"))
    for width in widths:
        ET.SubElement(grid, tag("gridCol"), {tag("w"): str(width)})
    all_rows = [[[(header, True)] for header in headers], *rows]
    for row_index, values in enumerate(all_rows):
        tr = ET.SubElement(element, tag("tr"))
        tr_props = ET.SubElement(tr, tag("trPr"))
        if row_index == 0:
            ET.SubElement(tr_props, tag("tblHeader"), {tag("val"): "true"})
        else:
            ET.SubElement(tr_props, tag("cantSplit"))
        for col_index, content in enumerate(values):
            shade = "EAF0F7" if row_index and col_index == 0 else None
            tr.append(cell(content, widths[col_index], header=row_index == 0, shade=shade))
    return element


def finding_lines(finding: dict) -> list[list[tuple[str, bool]]]:
    severity = finding.get("severity", "unrated")
    category = CATEGORY_LABELS.get(finding.get("category", ""), finding.get("category", ""))
    title = finding.get("title", "未命名问题")
    status = finding.get("status")
    status_label = "待核实" if status == "unverified" else "环境受限" if status == "environment_limited" else severity
    label = f"[{status_label}] {title}"
    locations = []
    evidence_text = []
    for item in finding.get("evidence", []):
        path = item.get("path")
        if path:
            line = item.get("line")
            locations.append(f"{path}:{line}" if line else path)
        elif item.get("url"):
            locations.append(item["url"])
        observation = item.get("observation")
        if observation:
            evidence_text.append(observation)
    location_lines = [(loc, False) for loc in dict.fromkeys(locations)] or [("位置未提供", False)]
    actual_lines = []
    for key, label_text in (("expected", "预期"), ("actual", "实际")):
        value = finding.get(key)
        if value:
            actual_lines.append((f"{label_text}：{value}", False))
    actual_lines.extend((f"证据：{text}", False) for text in evidence_text)
    impact = finding.get("impact", "")
    action = finding.get("suggested_action", "")
    action_lines = [(f"类型：{category}", False)]
    if impact:
        action_lines.append((f"影响：{impact}", False))
    if action:
        action_lines.append((f"建议：{action}", False))
    reproduction = finding.get("reproduction", {})
    if reproduction.get("command"):
        action_lines.append((f"复现：{reproduction['command']}", False))
    if reproduction.get("observed"):
        action_lines.append((f"观察：{reproduction['observed']}", False))
    return [[(label, True)], location_lines, actual_lines or [("证据详见位置列", False)], action_lines]


def write_docx(data: dict, output: Path) -> None:
    review = data["review"]
    findings = data.get("findings", [])
    confirmed = sum(item["status"] == "confirmed" for item in findings)
    unverified = sum(item["status"] == "unverified" for item in findings)
    limited = sum(item["status"] == "environment_limited" for item in findings)
    repo = review["repository"]["name"]
    revision = review["revision"]["head"]
    body = ET.Element(tag("body"))
    body.append(paragraph(f"{repo} 代码审查报告", style="Title", bold=True, color="17365D", size=34, keep_next=True))
    body.append(paragraph(f"审查版本 {revision}", style="Subtitle", color="526579", size=21, keep_next=True))
    body.append(paragraph("审查结论", style="Heading1", bold=True, color="17365D", size=25, keep_next=True))
    if confirmed:
        conclusion = f"确认 {confirmed} 项问题，另有 {unverified} 项待核实、{limited} 项受环境限制。"
    else:
        conclusion = f"未确认缺陷；另有 {unverified} 项待核实、{limited} 项受环境限制。测试通过只说明已执行检查覆盖的范围。"
    body.append(paragraph(conclusion, size=20))
    body.append(paragraph("审查范围", style="Heading1", bold=True, color="17365D", size=25, keep_next=True))
    body.append(paragraph(review.get("scope_summary", "未提供范围说明。"), size=19))
    for note in review.get("coverage_notes", []):
        body.append(paragraph(f"覆盖说明：{note}", size=18, color="526579"))
    body.append(paragraph("检查结果", style="Heading1", bold=True, color="17365D", size=25, keep_next=True))
    check_rows = []
    for check in data.get("checks", []):
        status = {"passed": "通过", "failed": "失败", "environment_limited": "环境受限", "not_run": "未运行"}.get(check["status"], check["status"])
        result = check.get("summary", "")
        if check.get("exit_code") is not None:
            result = f"退出码 {check['exit_code']}；{result}"
        check_rows.append([[(check["command"], False)], [(status, True)], [(result, False)]])
    body.append(table(["检查命令", "状态", "结果"], check_rows, [2600, 1300, 6406]))
    body.append(paragraph("审查发现", style="Heading1", bold=True, color="17365D", size=25, keep_next=True))
    ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), CATEGORY_ORDER.get(f["category"], 9), f["title"].casefold()))
    finding_rows = [finding_lines(item) for item in ordered]
    if finding_rows:
        body.append(table(["问题", "代码位置", "预期、实际与证据", "影响与建议"], finding_rows, [1900, 2300, 3500, 2606]))
    else:
        body.append(paragraph("本次未记录确认问题或待核实线索。", size=19))
    body.append(paragraph("报告说明", style="Heading1", bold=True, color="17365D", size=25, keep_next=True))
    body.append(paragraph("文件行号对应本报告记录的审查版本。报告中的建议是待处理方案；只有在修改后执行相应检查并通过，才可称为已验证修复。", size=18, color="526579"))
    sect = ET.SubElement(body, tag("sectPr"))
    ET.SubElement(sect, tag("pgSz"), {tag("w"): "11906", tag("h"): "16838"})
    ET.SubElement(sect, tag("pgMar"), {tag("top"): "760", tag("right"): "760", tag("bottom"): "760", tag("left"): "760", tag("header"): "360", tag("footer"): "360", tag("gutter"): "0"})
    document = ET.Element(tag("document"))
    document.append(body)
    document_xml = ET.tostring(document, encoding="utf-8", xml_declaration=True)
    styles = ET.Element(tag("styles"))
    defaults = ET.SubElement(styles, tag("docDefaults"))
    rdefaults = ET.SubElement(defaults, tag("rPrDefault")); rpr = ET.SubElement(rdefaults, tag("rPr"))
    ET.SubElement(rpr, tag("rFonts"), {tag("ascii"): "Aptos", tag("hAnsi"): "Aptos", tag("eastAsia"): "Microsoft YaHei"})
    ET.SubElement(rpr, tag("sz"), {tag("val"): "20"})
    pdefaults = ET.SubElement(defaults, tag("pPrDefault")); ppr = ET.SubElement(pdefaults, tag("pPr"))
    ET.SubElement(ppr, tag("spacing"), {tag("after"): "100", tag("line"): "270", tag("lineRule"): "auto"})
    for style_id, name in (("Normal", "Normal"), ("Title", "Title"), ("Subtitle", "Subtitle"), ("Heading1", "heading 1"), ("TableText", "Table Text")):
        style = ET.SubElement(styles, tag("style"), {tag("type"): "paragraph", tag("styleId"): style_id})
        ET.SubElement(style, tag("name"), {tag("val"): name})
        if style_id == "Heading1":
            props = ET.SubElement(style, tag("pPr")); ET.SubElement(props, tag("keepNext")); ET.SubElement(props, tag("spacing"), {tag("before"): "220", tag("after"): "100"})
    styles_xml = ET.tostring(styles, encoding="utf-8", xml_declaration=True)
    content_types = b'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'''
    root_rels = b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    doc_rels = b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("word/_rels/document.xml.rels", doc_rels)
    # Parse the parts after writing so malformed XML is reported immediately.
    with ZipFile(output) as archive:
        ET.fromstring(archive.read("word/document.xml"))
        ET.fromstring(archive.read("word/styles.xml"))


def location_text(finding: dict) -> str:
    locations = []
    for evidence in finding.get("evidence", []):
        path = evidence.get("path")
        if path:
            line = evidence.get("line")
            label = f"{path}:{line}" if line else path
            if label not in locations:
                locations.append(label)
    return ", ".join(f"`{item}`" for item in locations[:3]) or "见证据链接"


def render_chat_summary(data: dict, limit: int = 5) -> str:
    review = data["review"]
    findings = data["findings"]
    confirmed = [f for f in findings if f["status"] == "confirmed"]
    unverified = [f for f in findings if f["status"] == "unverified"]
    env = [f for f in findings if f["status"] == "environment_limited"]
    code_issues = [f for f in confirmed if f["category"] not in {"maintenance_gap"}]
    maintenance = [f for f in confirmed if f["category"] == "maintenance_gap"]
    ordered = sorted(code_issues, key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), CATEGORY_ORDER.get(f["category"], 9), f["title"].casefold()))
    selected = ordered[:limit]
    remaining = len(ordered) - len(selected)
    repo = review["repository"]["name"]
    head = review["revision"]["head"]
    lines = [f"# {repo} 审查报告", ""]
    if confirmed:
        lines.append(f"**结论：** 确认 {len(confirmed)} 项问题，另有 {len(unverified)} 项待核实；详列 {len(selected)} 项主要代码/规则问题。")
    else:
        lines.append(f"**结论：** 未确认缺陷；本次审查了指定范围，仍有 {len(unverified)} 项待核实，测试通过不代表覆盖范围之外没有问题。")
    mode_label = "全仓库审查" if review["mode"] == "repository_audit" else "改动审查"
    lines.append(f"**范围：** `{repo}` · `{head}` · {mode_label}。")
    lines.extend(["", "## 确认的问题"])
    if selected:
        for finding in selected:
            label = CATEGORY_LABELS.get(finding["category"], finding["category"])
            impact = finding.get("impact", "").strip()
            lines.append(f"- **[{finding['severity']}] [{label}] {finding['title']}** — {impact}（{location_text(finding)}）")
    else:
        lines.append("- 本次未确认代码或规则缺陷。")
    if remaining:
        lines.append(f"- 另有 {remaining} 项确认问题未在摘要中展开；查看完整记录获取清单。")
    if maintenance:
        lines.extend(["", "## 文档维护"])
        for finding in maintenance[:2]:
            lines.append(f"- {finding['title']}（{location_text(finding)}）。")
        if len(maintenance) > 2:
            lines.append(f"- 另有 {len(maintenance)-2} 项文档维护问题。")
    if unverified:
        lines.extend(["", "## 待核实"])
        for finding in unverified[:2]:
            detail = (finding.get("actual") or finding.get("impact", "需要补充证据")).rstrip("。.!? ")
            lines.append(f"- {finding['title']}：{detail}。")
        if len(unverified) > 2:
            lines.append(f"- 另有 {len(unverified)-2} 项待核实线索。")
    if env:
        lines.extend(["", f"**环境限制：** {len(env)} 项检查/发现受环境影响，详见完整记录。"])
    checks = data.get("checks", [])
    passed = sum(c["status"] == "passed" for c in checks)
    failed = sum(c["status"] == "failed" for c in checks)
    limited = sum(c["status"] == "environment_limited" for c in checks)
    not_run = sum(c["status"] == "not_run" for c in checks)
    coverage = review.get("coverage_notes", [])
    check_text = f"检查 {len(checks)} 项：{passed} 通过、{failed} 失败、{limited} 环境受限、{not_run} 未运行。"
    if coverage:
        first_note = coverage[0].rstrip("。.!? ")
        check_text += " 未覆盖：" + first_note + "。"
    lines.extend(["", f"**检查与范围：** {check_text}"])
    return "\n".join(lines) + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path, help="Review JSON following schemas/review.schema.json")
    parser.add_argument("--output", type=Path, help="Output Word report path (default: review JSON path with .docx extension)")
    args = parser.parse_args()
    try:
        data = load_review(args.review)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    output = args.output or args.review.with_suffix(".docx")
    if output.suffix.lower() != ".docx":
        parser.error("--output must use the .docx extension")
    write_docx(data, output)
    print(f"Wrote Word report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
