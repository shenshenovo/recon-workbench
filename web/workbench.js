let projectData = null,
  projectLoading = false,
  inputReviewRevision = 0;
let currentStep = "images",
  followTaskId = params.get("task"),
  viewingTask = null,
  current = null,
  pointViewer = null,
  loadedResult = null,
  polling = false;
let previewURLs = [];
const fieldIds = [
  "name",
  "method",
  "input_type",
  "input",
  "resolution",
  "threads",
  "camera",
  "camera-params",
  "single-camera",
  "steps",
  "eval",
  "save-images",
  "viewer",
  "mvs-stage",
  "max-splats",
  "export-every",
  "eval-every",
  "seed",
];
const methodInfo = {
  sfm: [
    "SfM · 稀疏几何",
    "估计相机位姿与稀疏点云，适合先检查照片能否建立稳定的几何关系。",
  ],
  mvs: [
    "MVS · 点云、网格与纹理",
    "在 SfM 基础上恢复更密集的几何，可继续输出表面网格和纹理。",
  ],
  "3dgs": [
    "3DGS · 高斯场景",
    "复用相机位姿并训练高斯表示，侧重场景的新视角渲染。结果使用 Brush 查看。",
  ],
};
function options() {
  const model = $("camera").value;
  const order =
    model === "SIMPLE_RADIAL"
      ? "f,cx,cy,k"
      : model === "PINHOLE"
        ? "fx,fy,cx,cy"
        : model === "OPENCV"
          ? "fx,fy,cx,cy,k1,k2,p1,p2"
          : "fx,fy,cx,cy,k1,k2,k3,k4";
  $("camera-params-help").textContent =
    "顺序：" +
    order +
    (model === "OPENCV_FISHEYE"
      ? "。没有 EXIF 焦距的鱼眼照片需要提供可靠内参，否则几何验证可能全部失败。"
      : "。留空时由 COLMAP 从 EXIF 或默认值初始化。");
  const type = $("input_type").value;
  for (const item of $("method").options)
    item.disabled =
      (type === "dataset" && item.value !== "3dgs") ||
      (type === "sfm-output" && item.value === "sfm");
  if ($("method").selectedOptions[0].disabled)
    $("method").value = type === "dataset" ? "3dgs" : "mvs";
  const gs = $("method").value === "3dgs",
    mvs = $("method").value === "mvs";
  $("gs-options").hidden = !gs;
  $("mvs-options").hidden = !mvs;
  $("photo-options").hidden = type !== "images";
  const info = methodInfo[$("method").value];
  $("method-title").textContent = info[0];
  $("method-help").textContent = info[1];
}
function renderInputSummary() {
  const item = projectData?.image_sets.find(
    (item) => item.id === window.selectedSetId,
  );
  $("saved-input-info").textContent = !item
    ? ""
    : window.inputDraftCount != null
      ? "当前勾选 " + window.inputDraftCount + " 张，尚未保存。"
      : item.input_type === "images"
        ? item.selected_count != null
          ? "本次使用已保存的 " +
            item.selected_count +
            " 张；图片集共 " +
            item.count +
            " 张。"
          : "请检测并保存筛选。图片集共 " + item.count + " 张。"
        : inputCount(item);
  $("settings-count").textContent = item ? inputCount(item) : "尚未选择输入";
  $("settings-info").textContent = item
    ? item.name + " · " + item.path
    : "请先在图片步骤上传或选择输入。";
}
function renderProjectInputs() {
  const saved = window.selectedSetId;
  $("saved-input").replaceChildren(new Option("选择已保存的输入…", ""));
  for (const item of projectData?.image_sets || [])
    $("saved-input").append(
      new Option(item.name + " · " + inputCount(item), item.id),
    );
  $("saved-input").value = saved || "";
  renderInputSummary();
}
function sourcePreview(item) {
  $("saved-previews").replaceChildren();
  for (const image of item?.images.slice(0, 12) || []) {
    const img = document.createElement("img");
    img.src =
      "/api/source-image?set=" +
      encodeURIComponent(item.id) +
      "&name=" +
      encodeURIComponent(image.name);
    img.alt = image.name;
    img.loading = "lazy";
    $("saved-previews").append(img);
  }
}
function chooseInput(item) {
  if ($("input").value !== item.path) resetCameraPrior();
  window.selectedSetId = item.id;
  $("saved-input").value = item.id;
  if (item.input_type === "dataset") $("method").value = "3dgs";
  if (item.input_type === "sfm-output" && $("method").value === "sfm")
    $("method").value = "mvs";
  $("input_type").value = item.input_type;
  $("input").value = item.path;
  options();
  renderInputSummary();
  sourcePreview(item);
}
function updateInputReview(state) {
  const item = projectData?.image_sets.find(
    (item) => item.id === state.image_set_id,
  );
  if (!item) return;
  inputReviewRevision++;
  item.selected_count = state.selection?.names.length ?? null;
  item.selection_status = state.selection
    ? "saved"
    : state.report
      ? "pending"
      : "unchecked";
  if (state.report) item.count = state.report.counts.total;
  renderProjectInputs();
  if (state.image_set_id === window.selectedSetId) {
    const names = new Set(
      state.selection?.names ||
        state.report?.images
          .filter((image) => image.decodable)
          .map((image) => image.name) ||
        [],
    );
    sourcePreview({
      ...item,
      images: item.images.filter((image) => names.has(image.name)),
    });
  }
}
async function loadProjectDetail() {
  if (projectLoading) return;
  projectLoading = true;
  const id = window.activeProjectId,
    revision = inputReviewRevision;
  try {
    const data = await api("/api/project?id=" + id);
    if (id !== window.activeProjectId || revision !== inputReviewRevision)
      return;
    projectData = data;
    renderProjectInputs();
  } finally {
    projectLoading = false;
  }
}
function goStep(step) {
  if (step === "result" && !followTaskId) {
    message("先创建任务，或从项目记录打开已有结果。");
    return;
  }
  if (step === "settings" && !$("input").value.trim()) {
    message("请先上传照片或选择输入目录。", true);
    return;
  }
  if (
    step === "settings" &&
    $("input_type").value === "images" &&
    (!qualityState?.selection || qualityDirty || qualityBusy)
  ) {
    message("请先检测并保存当前照片的筛选结果。", true);
    return;
  }
  currentStep = step;
  for (const name of ["images", "settings", "result"]) {
    $("step-" + name).hidden = name !== step;
    $(
      name === "images"
        ? "input-workspace"
        : name === "settings"
          ? "settings-workspace"
          : "result-workspace",
    ).hidden = name !== step;
  }
  document
    .querySelectorAll("[data-step]")
    .forEach((el) => el.classList.toggle("active", el.dataset.step === step));
  if (step === "result" && pointViewer) pointViewer.draw();
}
for (const el of document.querySelectorAll("[data-step]"))
  el.onclick = () => goStep(el.dataset.step);
$("next-settings").onclick = () => goStep("settings");
$("method").onchange = options;
function resetCameraPrior() {
  $("camera-params").value = "";
  $("single-camera").checked = false;
}
$("camera").onchange = () => {
  resetCameraPrior();
  options();
};
$("input_type").onchange = options;
$("saved-input").onchange = () => {
  const item = projectData?.image_sets.find(
    (item) => item.id === $("saved-input").value,
  );
  if (item) chooseInput(item);
  else window.selectedSetId = null;
};
for (const id of ["input", "input_type"])
  $(id).addEventListener("change", () => {
    resetCameraPrior();
    window.selectedSetId = null;
    $("saved-input").value = "";
    renderInputSummary();
  });
$("method").addEventListener("change", () => {
  const item = projectData?.image_sets.find(
    (item) => item.id === window.selectedSetId,
  );
  if (item && item.input_type !== $("input_type").value) {
    window.selectedSetId = null;
    $("saved-input").value = "";
    renderInputSummary();
  }
});
function saveDraft() {
  if (viewingTask) return;
  const fields = {};
  for (const id of fieldIds)
    fields[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
  const draft = { fields, input: window.selectedSetId, step: currentStep };
  if (typeof qualityState !== "undefined" && qualityState?.report) {
    draft.fingerprint = qualityState.report.fingerprint;
    draft.dirty = qualityDirty;
    draft.chosen = [...qualityChosen];
    draft.policyDirty = qualityPolicyDirty;
    draft.thresholds = [
      "quality-blur",
      "quality-dark",
      "quality-bright",
      "quality-near",
    ].map((id) => $(id).value);
  }
  sessionStorage.setItem(
    "recon-draft-" + window.activeProjectId,
    JSON.stringify(draft),
  );
}
window.beforeProjectChange = () => {
  saveDraft();
  return (
    typeof qualityDirty === "undefined" ||
    !qualityDirty ||
    confirm("筛选尚未保存，草稿会保留在当前浏览器。切换项目吗？")
  );
};
window.addEventListener("beforeunload", saveDraft);
async function restoreDraft() {
  let draft;
  try {
    draft = JSON.parse(
      sessionStorage.getItem("recon-draft-" + window.activeProjectId) || "null",
    );
  } catch {}
  if (!draft) return;
  for (const [id, value] of Object.entries(draft.fields || {}))
    if ($(id)) {
      if ($(id).type === "checkbox") $(id).checked = value;
      else $(id).value = value;
    }
  options();
  const item = projectData?.image_sets.find((item) => item.id === draft.input);
  if (item) {
    await chooseInput(item);
    if (
      draft.dirty &&
      qualityState?.report?.fingerprint === draft.fingerprint
    ) {
      qualityChosen = new Set(
        draft.chosen.filter((name) =>
          qualityState.report.images.some(
            (item) => item.name === name && item.decodable,
          ),
        ),
      );
      qualityDirty = true;
      qualityPolicyDirty = !!draft.policyDirty;
      if (draft.thresholds)
        [
          "quality-blur",
          "quality-dark",
          "quality-bright",
          "quality-near",
        ].forEach((id, i) => ($(id).value = draft.thresholds[i]));
      renderQuality();
    }
  }
  if (draft.step === "settings") goStep("settings");
}
$("photos").onchange = () => {
  previewURLs.forEach(URL.revokeObjectURL);
  previewURLs = [];
  $("upload-preview").replaceChildren();
  $("upload-info").textContent =
    "已选择 " + $("photos").files.length + " 张照片";
  for (const file of [...$("photos").files].slice(0, 8)) {
    const img = document.createElement("img");
    const url = URL.createObjectURL(file);
    previewURLs.push(url);
    img.src = url;
    img.alt = file.name;
    $("upload-preview").append(img);
  }
};
$("upload").onclick = async () => {
  const files = [...$("photos").files];
  if (files.length < 2) return message("至少选择两张有重叠视角的照片。", true);
  if (new Set(files.map((f) => f.name)).size !== files.length)
    return message("存在同名文件，请先修改文件名。", true);
  $("upload").disabled = true;
  $("project-select").disabled = true;
  try {
    const batch = await api("/api/upload-new", {
      project_id: window.activeProjectId,
      name: $("name").value + " · 照片",
    });
    let completed = 0;
    for (const file of files) {
      $("upload-info").textContent =
        "正在上传 " + ++completed + " / " + files.length;
      const response = await fetch(
        "/api/upload-file?id=" +
          batch.id +
          "&name=" +
          encodeURIComponent(file.name),
        {
          method: "POST",
          headers: { "X-Local-Token": window.LOCAL_TOKEN },
          body: file,
        },
      );
      const data = await response.json();
      if (!response.ok) throw Error(data.error);
    }
    await loadProjectDetail();
    const item = projectData.image_sets.find(
      (item) => item.id === batch.image_set_id,
    );
    await chooseInput(item);
    $("camera").value = "SIMPLE_RADIAL";
    $("upload-info").textContent = files.length + " 张照片已上传";
    message("上传完成，请检测质量并确认筛选。");
  } catch (error) {
    message(error.message, true);
  } finally {
    $("upload").disabled = false;
    $("project-select").disabled = false;
  }
};
$("form").onsubmit = async (event) => {
  event.preventDefault();
  const payload = {
    project_id: window.activeProjectId,
    image_set_id: window.selectedSetId || null,
    name: $("name").value,
    method: $("method").value,
    input_type: $("input_type").value,
    input: $("input").value,
    steps: +$("steps").value,
    "max-image-size": +$("resolution").value,
    threads: +$("threads").value,
    camera_model: $("camera").value,
    camera_params: $("camera-params").value.trim(),
    single_camera: $("single-camera").checked,
    mvs_stage: $("mvs-stage").value,
    eval_split_every: +$("eval").value,
    save_eval_images: $("save-images").checked,
    with_viewer: $("viewer").checked,
    "max-splats": +$("max-splats").value,
    "export-every": +$("export-every").value,
    "eval-every": +$("eval-every").value,
    seed: +$("seed").value,
  };
  $("start").disabled = true;
  try {
    const started = await api("/api/start", payload);
    saveDraft();
    followTaskId = started.task_id;
    viewingTask = null;
    clearResult();
    history.replaceState(
      {},
      "",
      routeURL("reconstruct", { task: followTaskId }),
    );
    goStep("result");
    await poll();
  } catch (error) {
    message(error.message, true);
    $("start").disabled = false;
  }
};
async function loadCloud() {
  if (!current || current.method === "3dgs") return;
  try {
    if (!pointViewer)
      pointViewer = new PointViewer($("point-canvas"), $("cloud-info"));
    await pointViewer.load(current.id, $("cloud-kind").value);
  } catch (error) {
    $("cloud-info").textContent = error.message;
  }
}
$("cloud-kind").onchange = loadCloud;
$("cloud-reset").onclick = () => pointViewer?.fit(false);
$("cloud-fit").onclick = () => pointViewer?.fit(true);
$("point-size").oninput = () => {
  if (pointViewer) {
    pointViewer.size = +$("point-size").value;
    pointViewer.draw();
  }
};
async function renderResult(task) {
  const rows = await api("/api/runs?project_id=" + task.project_id);
  const row = rows.find((row) => row.task_id === task.id);
  if (!row) return;
  current = row;
  loadedResult = task.id;
  $("open-folder").hidden = false;
  $("open-viewer").hidden = row.method !== "3dgs";
  $("metrics").hidden = false;
  $("metrics").replaceChildren();
  for (const [name, value] of [
    ["注册照片", row.registered_images],
    ["稀疏点数", row.sparse_points],
    ["稠密点数", row.dense_points],
    ["高斯数", row.splats],
  ]) {
    const box = document.createElement("div");
    box.className = "metric";
    const label = document.createElement("span");
    label.textContent = name;
    const number = document.createElement("b");
    number.textContent = fmt(value, 0);
    box.append(label, number);
    $("metrics").append(box);
  }
  $("point-section").hidden = row.method === "3dgs";
  $("result-empty").hidden = row.method !== "3dgs";
  if (row.method === "3dgs")
    $("result-empty").replaceChildren(
      Object.assign(document.createElement("p"), {
        textContent:
          "高斯模型已生成。点击“用 Brush 查看”浏览场景；渲染指标和曲线在实验对比页查看。",
      }),
    );
  else await loadCloud();
  $("previews").replaceChildren();
  if (row.method === "3dgs") {
    const previews = await api(
      "/api/previews?run=" + encodeURIComponent(row.id),
    );
    for (const path of previews.slice(-4)) {
      const img = document.createElement("img");
      img.src = "/api/image?path=" + encodeURIComponent(path);
      img.alt = "评估渲染";
      img.loading = "lazy";
      $("previews").append(img);
    }
  }
}
async function showTask(task, job) {
  viewingTask = task;
  $("task-name").textContent = task.name;
  $("detail-name").textContent = task.name;
  $("status").textContent =
    taskStatus[job?.status || task.status] || task.status;
  $("result-mode").textContent =
    task.status === "running" ? "RUNNING TASK" : "SAVED RESULT";
  $("task-path").textContent = task.output;
  $("task-error").textContent = task.error || "";
  $("task-parameters").textContent = JSON.stringify(task.parameters, null, 2);
  $("task-summary").replaceChildren();
  for (const [label, value] of [
    ["重建路线", task.method],
    [
      "实际输入",
      task.parameters.selected_images
        ? task.parameters.selected_images.length + " 张照片"
        : task.parameters.input_type || "已有输入",
    ],
    ["状态", taskStatus[task.status]],
    ["耗时", fmt(task.elapsed) + " s"],
  ]) {
    const line = document.createElement("div");
    const span = document.createElement("span");
    span.textContent = label;
    line.append(span, document.createTextNode(value));
    $("task-summary").append(line);
  }
  $("log").textContent = job?.log || task.log_text || "暂无运行日志";
  $("stages").replaceChildren();
  for (const state of job?.states || task.states || []) {
    const p = document.createElement("div");
    p.className = "stage";
    p.textContent = [
      state.path,
      taskStatus[state.status] || state.status,
      state.stage,
      state.error,
    ]
      .filter(Boolean)
      .join(" · ");
    $("stages").append(p);
  }
  const running = (job?.status || task.status) === "running";
  $("cancel").disabled = !running || !job;
  $("cancel").hidden = !running;
  $("execution-info").hidden = task.status === "completed";
  $("start").disabled = running;
  $("progress").hidden = !running;
  if (job?.progress)
    $("progress").value = (job.progress.step / job.progress.total) * 100;
  else if (running) $("progress").removeAttribute("value");
  if (["failed", "interrupted"].includes(task.status))
    $("task-log").open = true;
  if (task.status === "completed" && loadedResult !== task.id)
    await renderResult(task);
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const job = await api("/api/job");
    renderGlobalJob(job);
    $("start").disabled = job.status === "running";
    if (followTaskId) {
      const task = await api("/api/task?id=" + followTaskId);
      await showTask(task, job.task_id === followTaskId ? job : null);
    }
  } catch (error) {
    message(error.message, true);
  } finally {
    polling = false;
  }
}
$("cancel").onclick = async () => {
  await api("/api/cancel", {});
  message("已发送停止请求，已有结果会保留。");
  await poll();
};
for (const [id, viewer] of [
  ["open-folder", false],
  ["open-viewer", true],
])
  $(id).onclick = async () => {
    try {
      await api("/api/open", { run: current.id, viewer });
    } catch (error) {
      message(error.message, true);
    }
  };
function clearResult() {
  current = null;
  loadedResult = null;
  $("point-section").hidden = true;
  $("open-folder").hidden = true;
  $("open-viewer").hidden = true;
  $("metrics").hidden = true;
  $("previews").replaceChildren();
  $("result-empty").hidden = false;
  $("result-empty").replaceChildren(
    Object.assign(document.createElement("p"), {
      textContent: "运行中可查看阶段与日志，完成后在这里显示结果。",
    }),
  );
  $("task-error").textContent = "";
  if (pointViewer) {
    pointViewer.count = 0;
    pointViewer.original = null;
    pointViewer.draw();
  }
}
$("new-task").onclick = () => {
  followTaskId = null;
  viewingTask = null;
  clearResult();
  history.replaceState({}, "", routeURL("reconstruct"));
  goStep("images");
};
async function browse(path) {
  const data = await api("/api/folders?path=" + encodeURIComponent(path));
  $("folder-path").textContent = data.path;
  $("folders").replaceChildren();
  for (const item of data.folders)
    $("folders").append(button(item.name, () => browse(item.path)));
}
$("browse").onclick = async () => {
  try {
    await browse("data");
    $("dialog").showModal();
  } catch (error) {
    message(error.message, true);
  }
};
$("folder-root").onclick = () => browse("");
$("folder-up").onclick = () =>
  browse($("folder-path").textContent.split("/").slice(0, -1).join("/"));
$("folder-choose").onclick = () => {
  resetCameraPrior();
  $("input").value = $("folder-path").textContent;
  window.selectedSetId = null;
  $("saved-input").value = "";
  renderInputSummary();
  $("dialog").close();
};
$("folder-close").onclick = () => $("dialog").close();
async function loadPage() {
  window.selectedSetId = null;
  projectData = null;
  $("form").reset();
  options();
  previewURLs.forEach(URL.revokeObjectURL);
  previewURLs = [];
  $("upload-preview").replaceChildren();
  $("saved-previews").replaceChildren();
  followTaskId = new URLSearchParams(location.search).get("task");
  viewingTask = null;
  clearResult();
  goStep("images");
  resetQuality();
  await loadProjectDetail();
  if (followTaskId) {
    const task = await api("/api/task?id=" + followTaskId);
    if (task.project_id !== window.activeProjectId) {
      window.activeProjectId = task.project_id;
      $("project-select").value = task.project_id;
      syncNav("reconstruct");
      localStorage.setItem("workbench-project", task.project_id);
      history.replaceState({}, "", routeURL("reconstruct", { task: task.id }));
      await loadProjectDetail();
    }
    goStep("result");
    await poll();
    return;
  }
  const selected = params.get("input");
  const item = projectData.image_sets.find((item) => item.id === selected);
  if (item) await chooseInput(item);
  else await restoreDraft();
  const job = await api("/api/job");
  if (job.status === "running" && job.task_id) {
    const task = await api("/api/task?id=" + job.task_id);
    if (task.project_id === window.activeProjectId) {
      followTaskId = task.id;
      goStep("result");
      await poll();
    }
  }
}
options();
initShell("reconstruct", loadPage).catch((error) =>
  message(error.message, true),
);
setInterval(poll, 2000);
