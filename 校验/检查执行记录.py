#!/usr/bin/env python3
"""只读检查执行记录的结构与声明；不执行命令，不证明授权/证据真实。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

TOP = set("format_version is_template execution_id kind mode goal authorization_ref "
          "allowed_paths excluded_actions baselines candidates task_ids rule_set_version "
          "rule_decisions blockers required_checks checks review resume".split())
CHECK = set("id status repository commit command environment executed_at exit_code evidence_ref reason".split())
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
STATUSES = {"PASS", "FAIL", "NOT_RUN", "BLOCKED", "NOT_APPLICABLE"}


def need(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def obj(value: Any, keys: set[str], label: str) -> None:
    need(isinstance(value, dict), f"{label}必须是对象")
    need(set(value) == keys, f"{label}字段不符；缺少{keys - set(value)}，多余{set(value) - keys}")


def strings(value: Any, label: str) -> None:
    need(isinstance(value, list) and all(text(x) for x in value), f"{label}必须为非空字符串数组（允许空数组）")
    need(len(value) == len(set(value)), f"{label}存在重复值")


def versions(value: Any, label: str, allow_null: bool = False) -> None:
    need(isinstance(value, dict), f"{label}必须为仓库到提交的对象")
    for repo, sha in value.items():
        need(bool(REPO.fullmatch(repo)), f"{label}仓库名格式错误")
        need((allow_null and sha is None) or (isinstance(sha, str) and bool(SHA.fullmatch(sha))),
             f"{label}/{repo}需要完整小写commit SHA，不能用main或短版本")


def validate(data: Any, gate: str = "lint") -> None:
    """校验调用者声明；不能发现未声明依赖，也不访问GitHub或证据链接。"""
    need(gate in {"lint", "start", "verify"}, "未知检查模式")
    obj(data, TOP, "记录")
    need(type(data["format_version"]) is int and data["format_version"] == 1, "仅支持format_version=1")
    need(type(data["is_template"]) is bool, "is_template必须是布尔值")
    need(data["kind"] in ("DOCUMENTATION", "ENGINEERING", "BUSINESS"), "kind无效")
    need(data["mode"] in ("DISCOVER", "SIMULATION", "IMPLEMENTATION"), "mode无效")
    for key in ("execution_id", "goal", "authorization_ref", "rule_set_version"):
        need(data[key] is None or text(data[key]), f"{key}应为null或非空文本")
    for key in ("allowed_paths", "excluded_actions", "task_ids", "required_checks"):
        strings(data[key], key)
    need(all(re.fullmatch(r"TASK-\d{2}", x) for x in data["task_ids"]), "TASK编号格式错误")
    versions(data["baselines"], "baselines", allow_null=True)
    versions(data["candidates"], "candidates")

    writable = set()
    for item in data["allowed_paths"]:
        repo, sep, path = item.partition(":")
        need(bool(sep) and bool(REPO.fullmatch(repo)), "allowed_paths格式应为owner/repo:相对路径")
        p = PurePosixPath(path)
        need(text(path) and bool(p.parts) and not p.is_absolute() and ".." not in p.parts
             and path not in (".", "./") and not any(x in path for x in ("\\", "*", "?", ":")),
             "allowed_paths必须是具体相对文件/目录前缀，不支持跨目录或通配符")
        writable.add(repo)

    for key in ("rule_decisions", "blockers", "checks"):
        need(isinstance(data[key], list), f"{key}必须为数组")
    rule_ids = []
    for rule in data["rule_decisions"]:
        obj(rule, {"id", "status", "evidence_ref"}, "rule_decisions项")
        need(text(rule["id"]), "规则引用缺少id")
        need(rule["status"] in ("ADOPTED", "PENDING"), "规则状态必须为ADOPTED/PENDING")
        need(rule["evidence_ref"] is None or text(rule["evidence_ref"]), "规则证据引用类型错误")
        if rule["status"] == "ADOPTED":
            need(text(rule["evidence_ref"]), "已采纳规则必须引用实际采纳记录")
        rule_ids.append(rule["id"])
    need(len(rule_ids) == len(set(rule_ids)), "规则引用重复")

    for blocker in data["blockers"]:
        obj(blocker, {"summary", "blocks", "status", "resolution_ref"}, "blocker项")
        need(text(blocker["summary"]), "阻塞缺少说明")
        strings(blocker["blocks"], "blocker.blocks")
        need(bool(blocker["blocks"]) and set(blocker["blocks"]) <= {"SIMULATION", "IMPLEMENTATION", "VERIFY"},
             "阻塞需明确对应执行动作")
        need(blocker["status"] in ("OPEN", "RESOLVED"), "阻塞状态错误")
        need(blocker["resolution_ref"] is None or text(blocker["resolution_ref"]), "阻塞解决引用类型错误")
        if blocker["status"] == "RESOLVED":
            need(text(blocker["resolution_ref"]), "已解决阻塞缺少证据")

    checks = {}
    for check in data["checks"]:
        obj(check, CHECK, "check项")
        need(text(check["id"]) and check["id"] not in checks, "检查id缺失或重复")
        need(check["status"] in STATUSES, "检查状态无效")
        for key in CHECK - {"id", "status", "exit_code"}:
            need(check[key] is None or text(check[key]), f"check.{key}类型错误")
        need(check["exit_code"] is None or type(check["exit_code"]) is int, "退出码必须为整数或null")
        if check["status"] in ("PASS", "FAIL"):
            for key in ("repository", "commit", "command", "environment", "executed_at", "evidence_ref"):
                need(text(check[key]), f"已执行检查缺少{key}")
            versions({check["repository"]: check["commit"]}, "检查候选版本")
            when = datetime.fromisoformat(check["executed_at"].replace("Z", "+00:00"))
            need(when.tzinfo is not None and when.utcoffset() is not None, "执行时间必须带时区")
            need(type(check["exit_code"]) is int, "已执行检查缺少退出码")
            need((check["exit_code"] == 0) == (check["status"] == "PASS"), "结果与退出码冲突")
        else:
            need(check["exit_code"] is None and check["executed_at"] is None, "未执行项不能伪填退出码或执行时间")
            if check["status"] in ("BLOCKED", "NOT_APPLICABLE"):
                need(text(check["reason"]), "受阻/不适用必须说明理由")
        checks[check["id"]] = check

    review = data["review"]
    obj(review, {"status", "reviewer", "evidence_ref", "candidates", "limitations"}, "review")
    need(review["status"] in ("PASS", "FAIL", "NOT_RUN", "BLOCKED"), "审查状态无效")
    versions(review["candidates"], "审查版本")
    strings(review["limitations"], "审查限制")
    for key in ("reviewer", "evidence_ref"):
        need(review[key] is None or text(review[key]), f"review.{key}类型错误")
    if review["status"] in ("PASS", "FAIL"):
        need(text(review["reviewer"]) and text(review["evidence_ref"]) and bool(review["candidates"]),
             "已审查必须有审查者、证据和候选版本")
    obj(data["resume"], {"next_action", "remaining", "attempts"}, "resume")
    need(data["resume"]["next_action"] is None or text(data["resume"]["next_action"]), "下一动作类型错误")
    strings(data["resume"]["remaining"], "未完成项")
    strings(data["resume"]["attempts"], "已尝试动作")
    if gate == "lint":
        return

    need(not data["is_template"], "模板不能作为真实开工/验证记录")
    need(data["mode"] != "DISCOVER", "调查记录不能宣称实施就绪")
    for key in ("execution_id", "goal", "authorization_ref"):
        need(text(data[key]), f"开工缺少{key}")
    need(bool(writable) and bool(data["required_checks"]), "开工需要允许路径与必需验证计划")
    need("canasher/yixi-spec" in data["baselines"], "缺少业务规范仓基线")
    versions(data["baselines"], "实际基线")
    need(writable <= set(data["baselines"]), "存在未固定基线的允许修改仓库")
    for blocker in data["blockers"]:
        if blocker["status"] == "OPEN":
            need(data["mode"] not in blocker["blocks"], "存在阻断当前执行模式的未解决问题")
            if gate == "verify":
                need("VERIFY" not in blocker["blocks"], "存在阻断验证的未解决问题")
    if data["mode"] == "IMPLEMENTATION":
        need(all(r["status"] == "ADOPTED" for r in data["rule_decisions"]), "正式实现仍有待采纳规则")
        if data["kind"] == "BUSINESS":
            need(bool(data["task_ids"]) and text(data["rule_set_version"]) and bool(data["rule_decisions"]),
                 "正式业务实现缺少TASK/规则版本/适用采纳记录")
    if gate == "start":
        return

    need(writable == set(data["candidates"]), "输出候选版本须与声明的修改仓库一致")
    need(set(data["required_checks"]) <= set(checks), "缺少必需检查的实际执行结果")
    need(not any(c["status"] == "FAIL" for c in checks.values()), "存在未处理的失败结果")
    covered = set()
    for check_id in data["required_checks"]:
        check = checks[check_id]
        need(check["status"] == "PASS", "必需检查未实际通过，跳过/受阻不是通过")
        need(check["repository"] in writable, "必需检查没有对应本批修改仓库")
        need(check["commit"] == data["candidates"][check["repository"]], "检查结果属于旧候选版本")
        covered.add(check["repository"])
    need(covered == writable, "存在没有必需检查覆盖的修改仓库")
    need(review["status"] == "PASS" and review["candidates"] == data["candidates"], "当前候选版本的审查未通过")


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        need(key not in result, f"JSON字段重复：{key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"不允许非标准JSON数值：{value}")


def read_record(path: Path) -> dict:
    need(path.stat().st_size <= 1024 * 1024, "记录超过1MiB，请将大日志放证据存储")
    raw = path.read_bytes()
    need(len(raw) <= 1024 * 1024, "记录超过1MiB，请将大日志放证据存储")
    need(not raw.startswith(b"\xef\xbb\xbf"), "记录应为UTF-8无BOM")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)


def self_test() -> None:
    """合成字段仅留在内存，不创建任何真实采纳、提交、审查或业务通过证据。"""
    template = read_record(Path(__file__).resolve().parents[1] / "大模型研发流程/模板/任务执行记录.json")
    base = deepcopy(template)
    repo = "canasher/yixi-spec"
    base.update(is_template=False, execution_id="synthetic-only", mode="IMPLEMENTATION",
                goal="合成检查器测试", authorization_ref="synthetic:not-an-authorization",
                allowed_paths=[repo + ":大模型研发流程/"], baselines={repo: "a" * 40},
                candidates={repo: "b" * 40}, required_checks=["docs"])
    base["checks"] = [dict(id="docs", status="PASS", repository=repo, commit="b" * 40,
                           command="synthetic-not-executed", environment="memory-only",
                           executed_at="2026-10-02T00:00:00+00:00", exit_code=0,
                           evidence_ref="synthetic:not-real-evidence", reason=None)]
    base["review"] = dict(status="PASS", reviewer="synthetic-test", evidence_ref="synthetic:review",
                          candidates={repo: "b" * 40}, limitations=["仅合成记录"])
    count = 0

    def case(label: str, record: dict, gate: str, expected: bool) -> None:
        nonlocal count
        try:
            validate(record, gate)
            actual = True
        except (ValueError, TypeError):
            actual = False
        need(actual == expected, f"自测与预期不符：{label}")
        count += 1
        print(f"自测通过：{label}")

    case("模板仅结构有效", template, "lint", True)
    case("模板不能开工", template, "start", False)
    case("合成开工记录", base, "start", True)
    case("合成验证记录", base, "verify", True)
    mutations = [
        ("缺授权", lambda x: x.update(authorization_ref=None)),
        ("移动基线", lambda x: x["baselines"].update({repo: "main"})),
        ("候选版本变化", lambda x: x["candidates"].update({repo: "c" * 40})),
        ("通过无证据", lambda x: x["checks"][0].update(evidence_ref=None)),
        ("缺必需结果", lambda x: x.update(checks=[])),
        ("通过但退出码失败", lambda x: x["checks"][0].update(exit_code=1)),
        ("布尔退出码", lambda x: x["checks"][0].update(exit_code=False)),
        ("审查旧版本", lambda x: x["review"]["candidates"].update({repo: "d" * 40})),
        ("重复结果", lambda x: x["checks"].append(deepcopy(x["checks"][0]))),
        ("跨目录路径", lambda x: x.update(allowed_paths=[repo + ":../private"])),
        ("未固定写入仓库", lambda x: x["allowed_paths"].append("canasher/yixi-web:apps/")),
        ("业务缺规则", lambda x: x.update(kind="BUSINESS")),
        ("未知字段", lambda x: x.update(ready=True)),
        ("时间缺时区", lambda x: x["checks"][0].update(executed_at="2026-10-02T00:00:00")),
        ("未审查", lambda x: x["review"].update(status="NOT_RUN")),
    ]
    for label, mutate in mutations:
        changed = deepcopy(base)
        mutate(changed)
        case(label, changed, "verify", False)
    for status in ("NOT_RUN", "BLOCKED", "NOT_APPLICABLE"):
        changed = deepcopy(base)
        changed["checks"][0].update(status=status, exit_code=None, executed_at=None, reason="合成未执行")
        case("必需项不能用" + status + "替代", changed, "verify", False)
    changed = deepcopy(base)
    changed.update(kind="BUSINESS", task_ids=["TASK-07"], rule_set_version="synthetic")
    changed["rule_decisions"] = [dict(id="PD-023", status="PENDING", evidence_ref=None)]
    changed["blockers"] = [dict(summary="合成待定", blocks=["IMPLEMENTATION"], status="OPEN", resolution_ref=None)]
    case("正式业务未决阻塞", changed, "start", False)
    changed["mode"] = "SIMULATION"
    case("未决业务允许隔离模拟", changed, "start", True)
    changed["blockers"][0]["blocks"].append("VERIFY")
    case("明确验证阻塞仍拒绝", changed, "verify", False)
    print(f"共{count}项检查器合成自测通过；未执行任何真实业务命令。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, nargs="?")
    parser.add_argument("--gate", choices=("lint", "start", "verify"), default="lint")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        need(args.record is None and args.gate == "lint", "自测不接受记录或gate参数")
        self_test()
    else:
        need(args.record is not None, "请提供记录文件或--self-test")
        validate(read_record(args.record), args.gate)
        print(f"{args.gate}记录字段及声明一致性检查通过。")
    print("限制：未核验授权、提交与证据真实性或检查覆盖；不构成合入、人工验收、发布或商业启用许可。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError) as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        sys.exit(1)
