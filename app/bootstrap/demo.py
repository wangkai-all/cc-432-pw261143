"""Guided end-to-end demonstration driven entirely through HTTP APIs.

Run services first:  python -m app.run_all &
Then:                python -m app.bootstrap.demo

The script exercises every requirement: baseline build -> real analytics ->
sealed audit -> notification-loss simulation -> reconcile -> impact
decision (block vs rebuild) -> cache/export handling -> explanation
package privacy verification -> lineage trace.
"""
from __future__ import annotations

import json
import sys
import time

from app.common import httpclient

SNAP = "http://127.0.0.1:8071"
BUILDER = "http://127.0.0.1:8072"
REPORT = "http://127.0.0.1:8073"


def step(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"▶ {title}")
    print("=" * 72)


def show(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:2400])


def wait_health() -> None:
    for base in (SNAP, BUILDER, REPORT):
        for _ in range(50):
            try:
                httpclient.get(f"{base}/health")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit(f"service not ready: {base}")


def main() -> None:
    wait_health()

    step("1. 基线：当前授权快照与授权校验")
    snap = httpclient.get(f"{SNAP}/snapshots/current")
    print(f"当前快照 {snap['snapshot_id']}，授权 {len(snap['grants'])} 项，"
          f"指纹 {snap['fingerprint'][:16]}…")

    step("2. 构建样本：服务端授权闸门 + 逐行血缘（原始值不出服务）")
    job = httpclient.post(f"{BUILDER}/builds", {
        "source_id": "src_clinical_north", "purpose": "clinical",
        "org_scope": "north"})
    print(f"构建任务 {job['job_id']} → 阶段 {job['stage']}，"
          f"样本 {job['sample_id']}，检查点 {job['checkpoint']}")
    sample = httpclient.get(f"{BUILDER}/samples/{job['sample_id']}")
    print(f"样本行数 {sample['row_count']}，首条血缘（无敏感值）：")
    show(sample["lineage"][0])

    step("3. 真实分析：指标由原始行计算，每个指标记录贡献血缘与子合计")
    report = httpclient.post(f"{REPORT}/reports", {
        "title": "北区临床合规分析", "sample_id": job["sample_id"]})
    for m in report["metrics"]:
        print(f"  {m['metric_name']} = {m['value']} {m['unit']} "
              f"（贡献行 {m['row_count']}，子合计 {m['subtotals']}）")

    step("4. 法定封存：审计工件保留但锁定，撤回后仍可核验不可再用")
    sealed = httpclient.post(
        f"{REPORT}/reports/{report['report_id']}/seal",
        {"reason": "许可缩减前法定审计保留"})
    print(f"封存 ID {sealed['sealed_id']}，工件 {sealed['path']}")

    step("5. 通知丢失模拟：构建器下一次撤回推送将被丢弃")
    httpclient.post(f"{SNAP}/sim/loss",
                    {"consumer": "builder", "count": 1})
    print("已注入丢失规则（consumer=builder, count=1）")

    step("6. 许可缩减：撤回 north/clinical 授权并生成新快照")
    retr = httpclient.post(f"{SNAP}/retractions", {
        "source_id": "src_clinical_north",
        "scope_purposes": ["clinical"], "scope_orgs": ["north"],
        "reason": "来源许可缩减"})
    print(f"撤回 {retr['retraction_id']}，吊销授权 "
          f"{retr['revoked_grant_ids']}，新快照 "
          f"{retr['new_snapshot']['snapshot_id']}")
    deliveries = httpclient.get(f"{SNAP}/sim/deliveries")["deliveries"]
    for d in deliveries[-2:]:
        print(f"  投递：{d['consumer']} → {d['status']}（{d['detail']}）")

    step("7. 对账恢复：拉取补偿把丢失事件重新投递给构建器并重放")
    pulled = httpclient.get(
        f"{SNAP}/events/pull?consumer=builder"
        "&event_type=license.retracted")
    print(f"拉取事件 {pulled['count']} 条，重新投递 "
          f"{pulled['redelivered']} 条")
    for consumer in (BUILDER, REPORT):
        replay = httpclient.post(f"{consumer}/inbox/consume", {})
        print(f"  {consumer} 重放 {replay['consumed']} 条 → {replay['results']}")

    step("8. 影响裁决：贡献度解释 + 重建/阻断（缓存隔离、下载吊销）")
    updated = httpclient.get(f"{REPORT}/reports/{report['report_id']}")
    print(f"报告状态：{updated['status']}；阻断依据："
          f"{updated['block_reason']}")
    findings = httpclient.get(
        f"{REPORT}/findings?report_id={report['report_id']}")["findings"]
    for f in findings:
        print(f"  指标 {f['metric_id'][:16]}… 总 {f['total_rows']} 行 / "
              f"受影响 {f['affected_rows']}（{f['contribution_pct']}%）→ "
              f"{f['decision']}：{f['decision_reason']}")
    exports = [e for e in httpclient.get(f"{REPORT}/exports")["exports"]
               if e["report_id"] == report["report_id"]]
    for e in exports:
        print(f"  导出 {e['kind']}/{e['status']} legal_hold={e['legal_hold']}")

    step("9. 解释包：仅聚合与血缘标识，敏感扫描零违规")
    pkg = httpclient.post(
        f"{REPORT}/reports/{report['report_id']}/explanation",
        {"retraction_id": retr["retraction_id"]})
    print(f"解释包 {pkg['package_id']}，敏感扫描 "
          f"{pkg['sensitive_scan']}")
    blob = json.dumps(pkg["package"])
    leaked = [t for t in ("full_name", "email", "Avery", "SUBJ-",
                          "synthetic.test") if t in blob]
    print(f"敏感 token 泄漏检查：{'无（通过）' if not leaked else leaked}")

    step("10. 端到端血缘：指标 → 报告 → 样本 → 原始行号")
    lineage_id = sample["lineage"][0]["lineage_id"]
    trace = httpclient.get(
        f"{REPORT}/lineage/trace/{lineage_id}")
    show({"lineage_id": trace["lineage_id"],
          "builder_lineage": trace["builder_lineage"],
          "consumed_by_metrics": [c["metric_name"]
                                  for c in trace["consumed_by"]]})

    step("11. 替代路径：未受影响的欺诈报告可走安全子集重建")
    fraud_job = httpclient.post(f"{BUILDER}/builds", {
        "source_id": "src_tx_fraud", "purpose": "fraud_detection",
        "org_scope": "north"})
    fraud_report = httpclient.post(f"{REPORT}/reports", {
        "title": "北区欺诈分析", "sample_id": fraud_job["sample_id"]})
    rebuilt = httpclient.post(
        f"{REPORT}/reports/{fraud_report['report_id']}/rebuild", {})
    print(f"替代报告 {rebuilt['new_report']['report_id']}，保留 "
          f"{rebuilt['retained_rows']} 行、移除 {rebuilt['removed_rows']} 行")
    print("\n演示完成。控制台：http://127.0.0.1:8080")


if __name__ == "__main__":
    sys.exit(main())
