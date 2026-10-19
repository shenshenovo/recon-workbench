let runs = [],
  tasks = new Map(),
  selected = new Set();
function isTest(row) {
  return /smoke|integration|(?:^|[-_])(?:ui|layout)[-_]/i.test(row.name);
}
async function loadExperiments() {
  const id = window.activeProjectId;
  const [rows, project] = await Promise.all([
    api("/api/runs?project_id=" + id),
    api("/api/project?id=" + id),
  ]);
  if (id !== window.activeProjectId) return;
  runs = rows;
  tasks = new Map(project.tasks.map((t) => [t.id, t]));
  const previous = $("compare-input").value;
  $("compare-input").replaceChildren(new Option("全部图片集 / 来源", ""));
  const sources = new Map();
  for (const row of rows) {
    const task = tasks.get(row.task_id);
    row.source = task?.image_set_id || row.input || "unknown";
    const item = project.image_sets.find(
      (item) => item.id === task?.image_set_id,
    );
    sources.set(row.source, item?.name || row.input || "来源未记录");
  }
  for (const [id, name] of sources)
    $("compare-input").append(new Option(name, id));
  if (sources.has(previous)) $("compare-input").value = previous;
  else if (sources.size) $("compare-input").value = sources.keys().next().value;
  renderRuns();
}
function visibleRuns() {
  return runs.filter(
    (row) =>
      (!$("compare-input").value || row.source === $("compare-input").value) &&
      (!$("compare-method").value ||
        row.method === $("compare-method").value) &&
      ($("show-smoke").checked || !isTest(row)),
  );
}
function renderRuns() {
  const rows = visibleRuns();
  selected = new Set(
    [...selected].filter((id) => rows.some((row) => row.id === id)),
  );
  $("runs").replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "select-run";
    checkbox.value = row.id;
    checkbox.checked = selected.has(row.id);
    checkbox.onchange = () => {
      if (checkbox.checked && selected.size >= 4) {
        checkbox.checked = false;
        message("一次最多比较 4 个实验，便于核对条件。");
        return;
      }
      checkbox.checked ? selected.add(row.id) : selected.delete(row.id);
      renderComparison();
    };
    cell(tr, checkbox);
    const task = tasks.get(row.task_id);
    cell(
      tr,
      button(row.name, () => task && openResult(task), "text-link"),
    );
    for (const value of [
      row.method,
      row.registration_ratio == null
        ? "—"
        : (row.registration_ratio * 100).toFixed(1) + "%",
      fmt(row.reprojection_error_px, 3),
      fmt(row.psnr_db),
      fmt(row.ssim, 4),
      fmt(row.elapsed_seconds),
      fmt(row.artifact_bytes / 1024 ** 2),
      row.comparison_group || "—",
    ])
      cell(tr, value);
    $("runs").append(tr);
  }
  if (!rows.length)
    $("runs").innerHTML =
      '<tr><td class="empty" colspan="10">当前条件下没有已完成实验。可切换来源或显示测试任务。</td></tr>';
  renderComparison();
}
function renderComparison() {
  const rows = runs.filter((row) => selected.has(row.id));
  $("comparison-detail").hidden = !rows.length;
  $("report").disabled = !rows.length;
  const note = $("comparison-note");
  note.className = "callout";
  if (!rows.length) {
    note.textContent =
      "选择 1～4 个已完成实验。先核对实际输入与计时范围，再解释指标；测试任务按名称推断，可手动显示。";
    return;
  }
  const warnings = [];
  if (
    rows.some((row) => !tasks.get(row.task_id)?.parameters.quality_fingerprint)
  )
    warnings.push("部分任务未记录图片内容指纹，输入一致性需人工核验。");
  const signatures = rows.map((row) => {
    const task = tasks.get(row.task_id);
    return JSON.stringify([
      task?.image_set_id,
      task?.parameters.quality_fingerprint,
      task?.parameters.selected_images,
    ]);
  });
  if (new Set(signatures).size > 1)
    warnings.push(
      "所选实验的已记录输入条件不同，不能把指标差异直接归因于算法。",
    );
  if (new Set(rows.map((row) => row.timing_scope)).size > 1)
    warnings.push("耗时统计范围不同，需区分整条路线与复用后的阶段。");
  const gs = rows.filter((row) => row.method === "3dgs");
  if (
    gs.length &&
    (!gs.every((row) => row.comparison_group) ||
      new Set(gs.map((row) => row.comparison_group)).size > 1)
  )
    warnings.push("3DGS 评估分组不一致或缺失，PSNR/SSIM 不作统一排名。");
  if (new Set(rows.map((row) => row.method)).size > 1)
    warnings.push("不同路线输出类型不同：点数、高斯数及重投影误差不互相替代。");
  note.textContent =
    warnings.join(" ") ||
    "所选项具备相同的已记录输入条件；仍需核对参数、版本和评估步数。";
  if (warnings.length) note.classList.add("warning");
  $("comparison-title").textContent = "已选 " + rows.length + " 个实验";
  const table = document.createElement("table");
  const header = document.createElement("tr");
  for (const value of [
    "实验",
    "实际输入",
    "分辨率",
    "训练 / 评估步数",
    "耗时范围",
    "输入指纹",
  ]) {
    const th = document.createElement("th");
    th.textContent = value;
    header.append(th);
  }
  table.append(header);
  for (const row of rows) {
    const task = tasks.get(row.task_id),
      tr = document.createElement("tr");
    for (const value of [
      row.name,
      task?.parameters.selected_images
        ? task.parameters.selected_images.length + " 张"
        : row.input_images
          ? row.input_images + " 张"
          : "复用输入 / 未记录",
      fmt(row.max_resolution, 0),
      fmt(row.steps, 0) + " / " + fmt(row.evaluated_step, 0),
      row.timing_scope === "unified_route" ? "当前路线" : "当前脚本",
      task?.parameters.quality_fingerprint?.slice(0, 12) || "未记录",
    ])
      cell(tr, value);
    table.append(tr);
  }
  $("comparison-configs").replaceChildren(table);
  $("curve-block").hidden = !gs.length;
  $("curve-note").hidden = !gs.length;
  const curve = gs[0]?.evaluation_curve || [];
  chart("psnr", curve, "psnr_db");
  chart("ssim", curve, "ssim");
  $("comparison-warnings").replaceChildren();
  for (const row of rows) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = row.name + "：" + (row.warnings || []).join(" ");
    $("comparison-warnings").append(p);
  }
}
for (const id of ["compare-input", "compare-method", "show-smoke"])
  $(id).onchange = renderRuns;
$("report").onclick = async () => {
  try {
    const result = await api("/api/report", { runs: [...selected] });
    message("报告已保存到本机：" + result.path);
  } catch (error) {
    message(error.message, true);
  }
};
initShell("experiments", async () => {
  selected.clear();
  await loadExperiments();
}).catch((error) => message(error.message, true));
