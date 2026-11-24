import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { table } from "../components/common/table.js";
import { ChangeKinds, DirectoryStates } from "../constants/states.js";
import { submit } from "../hooks/submit.js";
import { poll } from "../hooks/poll.js";
import { el } from "../utils/dom.js";
import { cell, download, time } from "../utils/format.js";

function versionBadge(version, status) {
  return el("span", {class: `version-badge version-${status}`, "data-testid": `version-${version}`},
    `V${version} · ${DirectoryStates[status] || status}`);
}

function registerForm(reload, config) {
  const form = el("form", {class: "stack"},
    field("证书编号", el("input", {name: "code", required: true, pattern: "[A-Z0-9][A-Z0-9-]{0,39}", placeholder: "YT-Q10"})),
    field("电梯槽位", el("input", {name: "lift", required: true, pattern: "[A-Z0-9][A-Z0-9-]{0,39}", placeholder: "YT-L10"})),
    field("失效日", el("input", {name: "expires_on", type: "date", required: true})),
    field("撤销标记", el("select", {name: "revoked"},
      [el("option", {value: "false"}, "有效"), el("option", {value: "true"}, "已撤销")])),
    el("button", {type: "submit"}, "补录 / 更正入库"), errorText());
  submit(form, async values => {
    await api.registerCertificate({
      code: values.get("code").trim(), lift: values.get("lift").trim(),
      expires_on: values.get("expires_on"), revoked: values.get("revoked") === "true",
    });
    await reload();
  });
  return form;
}

function revokeForm(reload) {
  const form = el("form", {class: "inline-form"},
    field("撤销编号", el("input", {name: "code", required: true, pattern: "[A-Z0-9][A-Z0-9-]{0,39}", placeholder: "YT-Q05"})),
    el("button", {type: "submit", class: "danger"}, "撤销证书"), errorText());
  submit(form, async values => {
    await api.revokeCertificate({code: values.get("code").trim()});
    await reload();
  });
  return form;
}

function rebuildForm(overview, reload) {
  const current = overview.serving_capacity;
  const form = el("form", {class: "inline-form"},
    field("新单桶容量（只能调小）", el("input", {
      name: "capacity", type: "number", min: 1, max: current - 1, required: true, value: Math.max(1, current - 1),
    })),
    el("button", {type: "submit", disabled: Boolean(overview.rebuilding)},
      overview.rebuilding ? "影子重建进行中" : "申请缩容重建"), errorText());
  submit(form, async values => {
    await api.requestRebuild({capacity: Number(values.get("capacity"))});
    await reload();
  });
  return form;
}

function probeForm(reload) {
  const form = el("form", {class: "stack"},
    field("巡检日（留空取今天）", el("input", {name: "review_date", type: "date"})),
    field("点查编号（逗号或换行分隔，同批固定同一版目录）",
      el("textarea", {name: "probes", rows: 3, required: true, placeholder: "YT-Q01, YT-Q05, YT-Q99"})),
    el("button", {type: "submit"}, "按当前在用目录点查"), errorText());
  submit(form, async values => {
    const probes = values.get("probes").split(/[,\n]/).map(item => item.trim()).filter(Boolean);
    const reviewDate = values.get("review_date") || null;
    await api.createProbeBatch(reviewDate ? {probes, review_date: reviewDate} : {probes});
    await reload();
  });
  return form;
}

function rebuildPanel(overview) {
  const task = overview.rebuilding;
  if (!task) return empty("当前没有重建任务。调小容量后，旧版目录继续服务，新版在影子里按变更流追平。");
  return el("div", {class: "stack", "data-testid": "rebuild-progress"},
    el("p", {}, versionBadge(task.version, task.status),
      el("span", {class: "muted"}, ` 目标容量 ${task.capacity} · 已追平变更 #${task.applied_seq} / 最新 #${overview.latest_seq}`)),
    el("progress", {max: 100, value: task.progress}, `${task.progress}%`),
    el("p", {class: "muted"}, task.note),
    el("p", {class: "muted"}, "中断后 worker 按 applied_seq 续建；收齐全部变更并核对通过才一次性切换，重试不会重复发布。"));
}

function versionsTable(versions) {
  return table(["版本", "状态", "单桶容量", "变更进度", "证据摘要", "说明", "发布时间"],
    versions.map(item => [
      `V${item.version}`,
      el("span", {class: `version-badge version-${item.status}`}, DirectoryStates[item.status] || item.status),
      item.capacity, `#${item.applied_seq}（${item.progress}%）`,
      el("code", {class: "digest"}, item.digest ? item.digest.slice(0, 16) : "—"),
      item.note || "—", time(item.published_at),
    ]), "目录版本");
}

function certificatesTable(rows) {
  return table(["编号", "电梯槽位", "失效日", "状态", "最后变更", "操作人"],
    rows.map(item => [item.code, item.lift, item.expires_on,
      item.revoked ? el("strong", {class: "danger"}, "已撤销") : "在册",
      `#${item.updated_seq}`, item.updated_by]), "证书登记");
}

function changesTable(rows) {
  return table(["序号", "编号", "变更", "撤销标记", "时间"],
    rows.map(item => [`#${item.seq}`, item.code, ChangeKinds[item.kind] || item.kind,
      cell(item.payload.revoked), time(item.created_at)]), "变更流");
}

function batchDetail(batch, issue) {
  if (!batch) return empty("提交一批点查后，这里显示整批固定使用的目录版本和逐条结论。");
  const result = batch.result;
  const exportButton = el("button", {type: "button", onclick: async () => {
    exportButton.disabled = true;
    issue.textContent = "";
    try {
      await download(await api.exportProbeBatch(batch.id), `probe-evidence-${batch.id}.json`);
    } catch (error) {
      issue.textContent = error.message;
    } finally {
      exportButton.disabled = false;
    }
  }}, "下载本批桶证据");
  return el("div", {class: "stack", "data-testid": "probe-detail"},
    el("div", {class: "pinned-version"},
      el("strong", {}, `批次 #${batch.id} 固定目录 V${batch.directory_version}`),
      el("p", {class: "muted"}, `容量 ${result.directory_capacity} · 巡检日 ${batch.review_date} · 证据摘要 ${batch.digest}`),
      el("p", {class: "muted"}, "同批复用同一版目录；即使中途发生切换，本批结论与已下载证据保持原样。"),
      exportButton),
    el("dl", {class: "metrics"}, Object.entries(result.metrics).map(([label, value]) =>
      el("div", {}, el("dt", {}, label), el("dd", {}, cell(value))))),
    table(["类别", "项目", "桶号", "局部深度", "桶内条数", "电梯或槽位", "结论"],
      result.rows.map(row => [row.类别, row.项目, row.桶号, row.局部深度, row.桶内条数,
        row.电梯或槽位 ?? "—", row.结论]), "点查明细"));
}

function batchesTable(batches, selectedId) {
  if (!batches.length) return empty("还没有点查批次。");
  return table(["批次", "固定目录版本", "巡检日", "点查条数", "提交时间", ""],
    batches.map(item => [
      el("a", {href: `#directory/${item.id}`, "aria-current": item.id === selectedId ? "true" : null}, `#${item.id}`),
      `V${item.directory_version}`, item.review_date, item.probes.length, time(item.created_at),
      el("a", {href: `#directory/${item.id}`}, "查看证据"),
    ]), "点查批次");
}

export async function directoryPage(config, user, route, signal) {
  const isOperator = user.role === "operator";
  let overview = await api.directoryOverview(signal);
  let [certRows, changeRows, batches] = await Promise.all([
    api.directoryCertificates(), api.directoryChanges(), api.probeBatches(),
  ]);
  let selectedId = route.id && batches.some(item => item.id === route.id)
    ? route.id : batches[0]?.id ?? null;
  let selected = selectedId ? await api.probeBatch(selectedId) : null;
  const issue = errorText();

  async function reload() {
    if (signal.aborted) return;
    try {
      overview = await api.directoryOverview(signal);
      [certRows, changeRows, batches] = await Promise.all([
        api.directoryCertificates(), api.directoryChanges(), api.probeBatches(),
      ]);
      if (!batches.some(item => item.id === selectedId)) {
        selectedId = batches[0]?.id ?? null;
      }
      selected = selectedId ? await api.probeBatch(selectedId) : null;
      const host = document.querySelector("#directory-workspace");
      if (host && !signal.aborted) host.replaceChildren(...build().childNodes);
    } catch (error) {
      if (!signal.aborted) issue.textContent = error.message;
    }
  }

  function build() {
    return el("div", {class: "stack"},
      el("section", {class: "panel"},
        el("h2", {}, "在用目录版本"),
        el("p", {}, versionBadge(overview.serving_version, "serving"),
          el("span", {class: "muted"},
            ` 单桶容量 ${overview.serving_capacity} · 在册证书 ${overview.certificate_count} 条 · 变更流 #${overview.latest_seq}`)),
        el("p", {class: "muted"}, "切换前点查始终走这一版；补录、更正、撤销已同步写入，旧目录也能查到已入库更正。")),
      section("缩容影子重建", rebuildPanel(overview), isOperator ? rebuildForm(overview, reload) : empty("仅资料操作员可申请缩容。")),
      section("点查批次（同批同一版目录）", probeForm(reload), batchDetail(selected, issue)),
      section("历史点查批次", batchesTable(batches, selectedId)),
      section("目录版本谱系（旧批次证据永不改写）", versionsTable(overview.versions)),
      isOperator ? section("补录 / 更正", registerForm(reload, config), revokeForm(reload)) : null,
      section("权威证书登记", certificatesTable(certRows)),
      section("变更流（重建按序号追平）", changesTable(changeRows.slice(0, 20))));
  }

  if (overview.rebuilding) poll(async () => {
    await reload();
    // 重建任务消失（已切换或失败）即停止轮询。
    return Boolean(overview.rebuilding) && !signal.aborted;
  }, signal, 900);

  return el("div", {class: "page"},
    pageHeading(config.modules[4], "在线版本化目录 · 缩容重建期间照常补录、撤销与点查"),
    issue, el("div", {id: "directory-workspace"}, build()));
}
