/* Web console: talks only to the gateway, which blocks /internal/* paths. */
"use strict";

const api = async (service, method, path, body) => {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(`/api/${service}${path}`, opts);
  const text = await resp.text();
  const data = text ? JSON.parse(text) : {};
  if (!resp.ok) throw Object.assign(new Error(
    data.message || data.error || `HTTP ${resp.status}`), { data });
  return data;
};

const toast = (msg, kind = "") => {
  const el = document.createElement("div");
  el.className = `toast-item ${kind}`;
  el.textContent = msg;
  document.getElementById("toast").appendChild(el);
  setTimeout(() => el.remove(), 5200);
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const badge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;
const short = (s, n = 18) => s && s.length > n ? s.slice(0, n) + "…" : s;

const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------- tabs */
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach(
      (b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    el(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

/* ------------------------------------------------------------- health */
async function pollHealth() {
  const svcs = [["snapshot", "授权快照"], ["builder", "样本构建器"],
                ["report", "报告服务"]];
  el("health").innerHTML = (await Promise.all(svcs.map(async ([k, name]) => {
    try {
      await api(k, "GET", "/health");
      return `<span class="pill ok">● ${name} 在线</span>`;
    } catch {
      return `<span class="pill down">● ${name} 不可用</span>`;
    }
  }))).join("");
}
setInterval(pollHealth, 5000);

/* ------------------------------------------------------------- catalog */
async function loadCatalog() {
  const { sources } = await api("snapshot", "GET", "/sources");
  const opts = sources.map((s) =>
    `<option value="${s.source_id}">${esc(s.name)} (${s.source_id})</option>`);
  el("build-source").innerHTML = opts;
  el("retr-source").innerHTML = opts;
  el("catalog-out").innerHTML = sources.map((s) => `
    <div class="card">
      <h4>${esc(s.name)} <code>${esc(s.source_id)}</code></h4>
      <div class="kv">
        <div><span>类别：</span>${esc(s.category)}</div>
        <div><span>记录数：</span>${s.record_count}</div>
        <div><span>敏感列：</span>${s.sensitive_columns.map(esc).join(", ")}</div>
      </div>
      <table><thead><tr><th>授权 ID</th><th>用途</th><th>组织</th><th>状态</th>
      </tr></thead><tbody>
      ${s.grants.map((g) => `<tr><td><code>${esc(g.grant_id)}</code></td>
        <td>${esc(g.purpose)}</td><td>${esc(g.org_scope)}</td>
        <td>${badge(g.status)}</td></tr>`).join("")}
      </tbody></table>
    </div>`).join("");
}

async function loadSnapshots() {
  const { snapshots } = await api("snapshot", "GET", "/snapshots");
  el("snapshots-out").innerHTML = snapshots.length
    ? `<table><thead><tr><th>快照</th><th>版本</th><th>指纹</th><th>说明</th>
        <th>时间</th></tr></thead><tbody>
        ${snapshots.map((s) => `<tr><td><code>${esc(s.snapshot_id)}</code></td>
          <td>v${s.version}</td><td><code>${short(s.fingerprint, 22)}</code></td>
          <td class="muted">${esc(s.note || "")}</td>
          <td class="muted">${esc(s.created_at)}</td></tr>`).join("")}
        </tbody></table>`
    : `<p class="muted">尚无快照</p>`;
}
el("btn-load-catalog").onclick = () =>
  loadCatalog().then(() => toast("数据源已刷新", "ok")).catch((e) => toast(e.message, "err"));
el("btn-snapshots").onclick = () =>
  loadSnapshots().catch((e) => toast(e.message, "err"));
el("btn-new-snapshot").onclick = () =>
  api("snapshot", "POST", "/admin/snapshots", { note: "manual" })
    .then((s) => { toast(`已生成快照 ${s.snapshot_id}`, "ok"); loadSnapshots(); })
    .catch((e) => toast(e.message, "err"));

/* ------------------------------------------------------------- builder */
async function refreshSampleSelect() {
  const { samples } = await api("builder", "GET", "/samples");
  el("rpt-sample").innerHTML = samples.map((s) =>
    `<option value="${s.sample_id}">${esc(s.sample_id)} — ${esc(s.purpose)}/` +
    `${esc(s.org_scope)} [${esc(s.status)}] (${s.row_count} 行)</option>`).join("");
}

async function loadJobs() {
  const { jobs } = await api("builder", "GET", "/builds");
  el("jobs-out").innerHTML = jobs.length ? jobs.map((j) => `
    <div class="card">
      <div class="kv">
        <div><span>任务：</span><code>${esc(j.job_id)}</code></div>
        <div><span>数据源：</span>${esc(j.source_id)}</div>
        <div><span>用途/组织：</span>${esc(j.purpose)} / ${esc(j.org_scope)}</div>
        <div><span>快照：</span><code>${esc(j.snapshot_id)}</code></div>
        <div><span>阶段：</span>${badge(j.stage)}</div>
        <div><span>检查点：</span>${j.checkpoint}</div>
        ${j.sample_id ? `<div><span>样本：</span><code>${esc(j.sample_id)}</code></div>` : ""}
        ${j.error ? `<div><span>错误：</span>${esc(j.error)}</div>` : ""}
      </div>
      <div class="bar"><div style="width:${j.progress}%"></div></div>
      <div class="progress-wrap">进度 ${j.progress}%</div>
      ${j.stage === "interrupted" ? `
      <div class="inline-actions" style="margin-top:8px">
        <button onclick="resumeJob('${j.job_id}')">从检查点恢复构建</button>
      </div>` : ""}
    </div>`).join("") : `<p class="muted">暂无构建任务</p>`;
}

async function loadSamples() {
  const { samples } = await api("builder", "GET", "/samples");
  el("samples-out").innerHTML = samples.length ? `
    <table><thead><tr><th>样本版本</th><th>数据源</th><th>用途</th><th>组织</th>
    <th>快照</th><th>行数</th><th>状态</th><th>撤回</th><th>替代</th></tr></thead>
    <tbody>${samples.map((s) => `<tr>
      <td><code>${short(s.sample_id, 20)}</code><br>v${s.version}</td>
      <td>${esc(s.source_id)}</td><td>${esc(s.purpose)}</td>
      <td>${esc(s.org_scope)}</td><td><code>${esc(s.snapshot_id)}</code></td>
      <td>${s.row_count}</td><td>${badge(s.status)}</td>
      <td class="muted">${esc(s.retraction_id ? short(s.retraction_id, 14) : "—")}</td>
      <td class="muted">${esc(s.superseded_by ? short(s.superseded_by, 14) : "—")}</td>
    </tr>`).join("")}</tbody></table>` : `<p class="muted">暂无样本</p>`;
  refreshSampleSelect().catch(() => {});
}

el("btn-build").onclick = async () => {
  try {
    const job = await api("builder", "POST", "/builds", {
      source_id: el("build-source").value,
      purpose: el("build-purpose").value,
      org_scope: el("build-org").value,
    });
    toast(`构建任务 ${job.job_id} 完成（阶段：${job.stage}）`, "ok");
    loadJobs(); loadSamples();
  } catch (e) { toast(e.message, "err"); }
};
el("btn-refresh-jobs").onclick = () =>
  loadJobs().catch((e) => toast(e.message, "err"));

window.resumeJob = async (jobId) => {
  try {
    const job = await api("builder", "POST", `/builds/${jobId}/run`, {});
    toast(`任务 ${jobId} 已恢复，阶段：${job.stage}`, "ok");
    loadJobs(); loadSamples();
  } catch (e) {
    toast(e.message, "err");
    loadJobs();
  }
};

/* ------------------------------------------------------------- purpose */
el("btn-pchg").onclick = async () => {
  try {
    const r = await api("builder", "POST", "/purpose-changes", {
      lineage_id: el("pchg-lineage").value.trim(),
      new_purpose: el("pchg-purpose").value,
    });
    toast(`用途变更已${r.status === "approved" ? "批准" : "拒绝"}：${r.status}`,
      r.status === "approved" ? "ok" : "err");
    loadPurposeChanges();
  } catch (e) { toast(e.message, "err"); }
};
async function loadPurposeChanges() {
  const { requests } = await api("builder", "GET", "/purpose-changes");
  el("pchg-out").innerHTML = requests.length ? `
    <table><thead><tr><th>请求</th><th>血缘</th><th>原用途→新用途</th>
    <th>组织</th><th>快照</th><th>裁决</th><th>拒绝原因</th></tr></thead><tbody>
    ${requests.map((r) => `<tr>
      <td><code>${short(r.request_id, 14)}</code></td>
      <td><code>${short(r.lineage_id, 26)}</code></td>
      <td>${esc(r.old_purpose)} → <b>${esc(r.new_purpose)}</b></td>
      <td>${esc(r.org_scope)}</td><td><code>${esc(r.snapshot_id)}</code></td>
      <td>${badge(r.status)}</td>
      <td class="muted">${esc(r.rejection_reason || "—")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">暂无用途变更记录</p>`;
}
el("btn-pchg-list").onclick = () =>
  loadPurposeChanges().catch((e) => toast(e.message, "err"));

/* ------------------------------------------------------------- reports */
async function loadReports() {
  const { reports } = await api("report", "GET", "/reports");
  const blocks = await Promise.all(reports.map(async (r) => {
    const full = await api("report", "GET", `/reports/${r.report_id}`);
    const metrics = full.metrics.map((m) => `
      <div class="metric">
        <div><b>${esc(m.metric_name)}</b> <span class="muted">(${esc(m.metric_type)})</span></div>
        <div class="val">${m.value ?? "—"} <span class="muted" style="font-size:12px">${esc(m.unit || "")}</span></div>
        <div class="muted">贡献行数 ${m.row_count} · 子合计
          <code>${esc(JSON.stringify(m.subtotals))}</code></div>
      </div>`).join("");
    return `<div class="card">
      <h4>${esc(r.title)} <code>${short(r.report_id, 18)}</code> v${r.version}
        ${badge(r.status)}</h4>
      <div class="kv">
        <div><span>用途/组织：</span>${esc(r.purpose)} / ${esc(r.org_scope)}</div>
        <div><span>样本：</span><code>${short(r.sample_id, 18)}</code></div>
        <div><span>快照：</span><code>${esc(r.snapshot_id)}</code></div>
        ${r.rebuilt_from ? `<div><span>重建自：</span><code>${short(r.rebuilt_from, 16)}</code></div>` : ""}
        ${r.block_reason ? `<div><span>阻断原因：</span>${esc(r.block_reason)}</div>` : ""}
      </div>
      ${metrics}
      <div class="inline-actions">
        ${r.status === "tainted" ? `<button onclick="rebuildReport('${r.report_id}')">从安全子集重建</button>` : ""}
        ${r.status === "blocked" ? `<span class="muted">已阻断：仅允许基于新授权样本生成替代报告</span>` : ""}
        <button class="secondary" onclick="sealReport('${r.report_id}')">封存审计（法定保留）</button>
        <button class="secondary" onclick="explainReport('${r.report_id}')">生成解释包（脱敏）</button>
        <button class="secondary" onclick="loadExports('${r.report_id}')">查看导出/下载状态</button>
      </div>
      <div id="exports-${r.report_id}"></div>
    </div>`;
  }));
  el("reports-out").innerHTML = blocks.join("") || `<p class="muted">暂无报告</p>`;
}

el("btn-rpt-create").onclick = async () => {
  try {
    const r = await api("report", "POST", "/reports", {
      title: el("rpt-title").value || "未命名分析报告",
      sample_id: el("rpt-sample").value,
    });
    toast(`报告已生成：${r.report_id}`, "ok");
    loadReports();
  } catch (e) { toast(e.message, "err"); }
};
el("btn-rpt-refresh").onclick = () =>
  loadReports().catch((e) => toast(e.message, "err"));

window.rebuildReport = async (id) => {
  try {
    const r = await api("report", "POST", `/reports/${id}/rebuild`, {});
    toast(`已生成替代报告，保留 ${r.retained_rows} 行、移除 ${r.removed_rows} 行`, "ok");
    loadReports();
  } catch (e) { toast(e.message, "err"); }
};
window.sealReport = async (id) => {
  try {
    const r = await api("report", "POST", `/reports/${id}/seal`,
      { reason: "法定审计保留（许可缩减前快照）" });
    toast(`审计已封存：${r.sealed_id}（保留但锁定，不可再用于分析）`, "ok");
    loadReports();
  } catch (e) { toast(e.message, "err"); }
};
window.explainReport = async (id) => {
  try {
    const r = await api("report", "POST", `/reports/${id}/explanation`, {});
    const offenses = r.sensitive_scan.offenses;
    toast(`解释包已生成，敏感扫描违规数：${offenses.length}`,
      offenses.length ? "err" : "ok");
    window.open("about:blank", "_blank").document.write(
      `<pre>${esc(JSON.stringify(r.package, null, 2))}</pre>`);
  } catch (e) { toast(e.message, "err"); }
};
window.loadExports = async (reportId) => {
  const { exports: all } = await api("report", "GET", "/exports");
  const rows = all.filter((x) => x.report_id === reportId);
  const box = el(`exports-${reportId}`);
  box.innerHTML = `<table style="margin-top:10px"><thead><tr><th>导出</th><th>类型</th>
    <th>状态</th><th>法定保留</th><th>操作</th></tr></thead><tbody>
    ${rows.map((x) => `<tr><td><code>${short(x.export_id, 14)}</code></td>
      <td>${esc(x.kind)}</td><td>${badge(x.status)}</td>
      <td>${x.legal_hold ? "是" : "否"}</td>
      <td><div class="inline-actions">
        <button class="secondary" onclick="tryDownload('${x.export_id}')">尝试下载</button>
      </div></td></tr>`).join("")}</tbody></table>`;
};
window.tryDownload = async (exportId) => {
  try {
    const body = await api("report", "GET",
      `/exports/${exportId}/download`);
    toast("下载成功：内容仅含聚合指标与血缘清单", "ok");
    window.open("about:blank", "_blank").document.write(
      `<pre>${esc(JSON.stringify(body, null, 2))}</pre>`);
  } catch (e) {
    toast(`下载被拒绝：${e.message}`, "err");
  }
};

/* ------------------------------------------------------------- retraction */
el("btn-retr").onclick = async () => {
  try {
    const purposes = el("retr-purposes").value.split(",").map((s) => s.trim()).filter(Boolean);
    const orgs = el("retr-orgs").value.split(",").map((s) => s.trim()).filter(Boolean);
    const r = await api("snapshot", "POST", "/retractions", {
      source_id: el("retr-source").value,
      scope_purposes: purposes, scope_orgs: orgs,
      reason: el("retr-reason").value,
    });
    toast(`撤回完成：${r.retraction_id}，吊销 ${r.revoked_grant_ids.length} 项授权，`
      + `新快照 ${r.new_snapshot.snapshot_id}`, "ok");
    loadRetractions(); loadSnapshots(); loadCatalog();
  } catch (e) { toast(e.message, "err"); }
};
async function loadRetractions() {
  const { retractions } = await api("snapshot", "GET", "/retractions");
  el("retractions-out").innerHTML = retractions.length ? `
    <table><thead><tr><th>撤回 ID</th><th>数据源</th><th>用途范围</th>
    <th>组织范围</th><th>原因</th><th>生效快照</th></tr></thead><tbody>
    ${retractions.map((r) => `<tr>
      <td><code>${short(r.retraction_id, 14)}</code></td>
      <td>${esc(r.source_id)}</td>
      <td>${r.scope_purposes.length ? esc(r.scope_purposes.join(",")) : "全部"}</td>
      <td>${r.scope_orgs.length ? esc(r.scope_orgs.join(",")) : "全部"}</td>
      <td>${esc(r.reason)}</td>
      <td><code>${esc(r.effective_snapshot_id)}</code></td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">暂无撤回记录</p>`;
}
el("btn-retr-list").onclick = () =>
  loadRetractions().catch((e) => toast(e.message, "err"));

async function loadFindings() {
  const { findings } = await api("report", "GET", "/findings");
  el("findings-out").innerHTML = findings.length ? `
    <table><thead><tr><th>报告</th><th>撤回</th><th>指标</th><th>总行</th>
    <th>受影响</th><th>安全</th><th>贡献度</th><th>裁决</th><th>依据</th>
    </tr></thead><tbody>${findings.map((f) => {
      const cls = f.contribution_pct > 25 ? "high" : f.contribution_pct > 0 ? "mid" : "";
      return `<tr>
        <td><code>${short(f.report_id, 14)}</code></td>
        <td><code>${short(f.retraction_id, 12)}</code></td>
        <td><code>${short(f.metric_id, 12)}</code></td>
        <td>${f.total_rows}</td><td>${f.affected_rows}</td>
        <td>${f.safe_rows}</td>
        <td><div>${f.contribution_pct}%</div>
          <div class="bar-wrap" role="img"
               aria-label="受影响贡献度 ${f.contribution_pct}%，裁决 ${f.decision}">
            <div class="bar-tooltip">受影响 ${f.affected_rows}/${f.total_rows} 行
              （${f.contribution_pct}%）· ${f.decision}：${esc(f.decision_reason)}</div>
            <div class="bar"><div class="${cls}" style="width:${Math.min(f.contribution_pct, 100)}%"></div></div>
          </div></td>
        <td>${badge(f.decision)}</td>
        <td class="muted">${esc(f.decision_reason)}</td></tr>`;
    }).join("")}</tbody></table>` : `<p class="muted">暂无影响明细（先在“故障模拟”页执行对账/重放，或事件已实时投递）</p>`;
}
el("btn-findings").onclick = () =>
  loadFindings().catch((e) => toast(e.message, "err"));

/* ------------------------------------------------------------- simulation */
el("btn-loss").onclick = async () => {
  try {
    const r = await api("snapshot", "POST", "/sim/loss", {
      consumer: el("loss-consumer").value,
      event_type: "license.retracted",
      count: Number(el("loss-count").value),
    });
    toast(`丢失规则已注入：${r.consumer} 下 ${r.remaining} 次事件将被丢弃`, "ok");
  } catch (e) { toast(e.message, "err"); }
};
el("btn-fail").onclick = async () => {
  try {
    const r = await api("builder", "POST", "/sim/fail", {
      fail_stage: el("fail-stage").value,
      count: Number(el("fail-count").value),
    });
    toast(`中断规则已注入：阶段 ${r.fail_stage}，剩余 ${r.remaining} 次`, "ok");
  } catch (e) { toast(e.message, "err"); }
};

el("btn-reconcile").onclick = async () => {
  try {
    const pulls = await Promise.all(
      ["builder", "report"].map((c) =>
        api("snapshot", "GET",
          `/events/pull?consumer=${c}&event_type=license.retracted`)));
    const replays = await Promise.all(
      ["builder", "report"].map((c) =>
        api(c, "POST", "/inbox/consume", {})));
    const pulled = pulls.reduce((n, p) => n + p.count, 0);
    toast(`对账完成：拉取补偿 ${pulled} 条事件，构建器重放 `
      + `${replays[0].consumed} 条、报告服务重放 ${replays[1].consumed} 条`, "ok");
    loadDeliveries(); loadInbox(); loadJobs(); loadSamples();
    loadReports(); loadFindings();
  } catch (e) { toast(e.message, "err"); }
};

async function loadDeliveries() {
  const { deliveries } = await api("snapshot", "GET", "/sim/deliveries");
  el("sim-out").innerHTML = `
    <h3 style="color:var(--muted)">出站投递状态</h3>
    <table><thead><tr><th>事件</th><th>类型</th><th>消费者</th><th>状态</th>
    <th>尝试</th><th>说明</th></tr></thead><tbody>
    ${deliveries.map((d) => `<tr>
      <td><code>${short(d.event_id, 16)}</code></td>
      <td>${esc(d.event_type)}</td><td>${esc(d.consumer)}</td>
      <td>${badge(d.status)}</td><td>${d.attempts}</td>
      <td class="muted">${esc(d.detail || "")}</td></tr>`).join("")}
    </tbody></table>`;
}
el("btn-deliveries").onclick = () =>
  loadDeliveries().catch((e) => toast(e.message, "err"));

async function loadInbox() {
  const [b, r] = await Promise.all([
    api("builder", "GET", "/inbox"), api("report", "GET", "/inbox")]);
  const row = (title, events) => `<div class="card"><h4>${title}</h4>
    <table><thead><tr><th>事件</th><th>类型</th><th>已消费</th><th>接收时间</th></tr></thead>
    <tbody>${events.map((e) => `<tr><td><code>${short(e.event_id, 16)}</code></td>
      <td>${esc(e.event_type)}</td>
      <td>${e.consumed ? badge("delivered") : badge("pending")}</td>
      <td class="muted">${esc(e.received_at)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">空</td></tr>`}</tbody></table></div>`;
  el("sim-out").innerHTML =
    row("构建器收件箱", b.events) + row("报告服务收件箱", r.events)
    + el("sim-out").innerHTML;
}
el("btn-inbox").onclick = () =>
  loadInbox().catch((e) => toast(e.message, "err"));

/* ------------------------------------------------------------- lineage */
el("btn-trace").onclick = async () => {
  try {
    const r = await api("report", "GET",
      `/lineage/trace/${encodeURIComponent(el("trace-id").value.trim())}`);
    el("trace-out").innerHTML = `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`;
  } catch (e) { toast(e.message, "err"); }
};

/* ------------------------------------------------------------- init */
(async function init() {
  await pollHealth();
  try {
    await loadCatalog();
    await loadSnapshots();
    await loadJobs();
    await loadSamples();
    await loadPurposeChanges();
    await loadReports();
    await loadRetractions();
    await loadFindings();
  } catch (e) { /* services may not be seeded yet */ }
})();
