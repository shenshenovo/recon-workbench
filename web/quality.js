/* Quality checks are advice; only the saved, explicit selection starts a job. */
const qualitySection = $("quality-section");
let qualityState = null,
  qualityChosen = new Set(),
  qualityDirty = false,
  qualityPolicyDirty = false,
  qualityBusy = false,
  qualityRequest = 0;
const qualityLabels = {
  decode_error: "无法解码",
  blurry: "疑似模糊 / 少纹理",
  underexposed: "疑似欠曝",
  overexposed: "疑似过曝",
  exact_duplicate: "文件完全重复",
  near_duplicate: "疑似相似视角",
};

function resetQuality() {
  qualityRequest++;
  qualityState = null;
  qualityChosen = new Set();
  qualityDirty = false;
  qualityPolicyDirty = false;
  window.inputDraftCount = null;
  renderInputSummary();
  $("quality-results").hidden = true;
  $("quality-save").disabled = true;
  $("quality-summary").textContent =
    $("input_type").value === "images"
      ? "尚未检测当前原始照片。"
      : "已有 SfM / Brush 数据集无需在此筛选；请在原始照片阶段检查质量。";
}

function qualityCount() {
  window.inputDraftCount = qualityDirty ? qualityChosen.size : null;
  renderInputSummary();
  $("quality-selected").textContent =
    "已选择 " +
    qualityChosen.size +
    " / " +
    qualityState.report.images.length +
    " 张 · " +
    (qualityDirty
      ? "尚未保存，保存后才能开始重建"
      : qualityState.selection
        ? "筛选已保存，重建将使用独立副本"
        : "请确认并保存筛选");
  $("quality-save").disabled = qualityBusy || qualityPolicyDirty;
}

function renderQuality() {
  const report = qualityState?.report;
  $("quality-results").hidden = !report;
  if (!report) {
    $("quality-save").disabled = true;
    $("quality-summary").textContent =
      "尚未检测。点击“检测当前图片集”，完成后勾选要使用的照片。";
    return;
  }
  const c = report.counts;
  $("quality-summary").textContent =
    "共 " +
    c.total +
    " 张，可解码 " +
    c.decodable +
    " 张；疑似模糊 " +
    c.blurry +
    "，欠曝 " +
    c.underexposed +
    "，过曝 " +
    c.overexposed +
    "，完全重复 " +
    c.exact_duplicate +
    "，疑似相似 " +
    c.near_duplicate +
    "。各类提示可能重叠。";
  $("quality-rows").replaceChildren();
  for (const item of report.images) {
    if ($("quality-issues").checked && !item.flags.length) continue;
    const row = document.createElement("tr"),
      use = document.createElement("td"),
      check = document.createElement("input");
    check.type = "checkbox";
    check.className = "quality-select";
    check.dataset.name = item.name;
    check.checked = qualityChosen.has(item.name);
    check.disabled = !item.decodable;
    check.onchange = () => {
      check.checked
        ? qualityChosen.add(item.name)
        : qualityChosen.delete(item.name);
      qualityDirty = true;
      qualityCount();
    };
    use.append(check);
    row.append(use);
    const preview = document.createElement("td");
    if (item.decodable) {
      const img = document.createElement("img");
      img.className = "quality-image";
      img.loading = "lazy";
      img.alt = item.name;
      img.src =
        "/api/source-image?set=" +
        encodeURIComponent(qualityState.image_set_id) +
        "&name=" +
        encodeURIComponent(item.name);
      preview.append(img);
    }
    const name = document.createElement("span");
    name.className = "quality-name";
    name.textContent = item.name;
    preview.append(name);
    row.append(preview);
    for (const value of [
      item.decodable ? item.width + " × " + item.height : "—",
      fmt(item.laplacian_variance),
      fmt(item.mean_luminance),
      item.decodable
        ? (item.dark_ratio * 100).toFixed(1) +
          "% / " +
          (item.bright_ratio * 100).toFixed(1) +
          "%"
        : "—",
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const flags = document.createElement("td");
    flags.className = "quality-flags";
    flags.textContent =
      item.flags.map((flag) => qualityLabels[flag]).join("；") ||
      "未发现上述问题";
    if (item.duplicate_of)
      flags.append(
        document.createTextNode("；与 " + item.duplicate_of + " 完全重复"),
      );
    if (item.similar_to)
      flags.append(
        document.createTextNode(
          "；疑似相似于 " + item.similar_to + "，距离 " + item.hash_distance,
        ),
      );
    if (item.error) flags.append(document.createTextNode("；" + item.error));
    row.append(flags);
    $("quality-rows").append(row);
  }
  qualityCount();
}

function acceptQuality(state) {
  qualityState = state;
  qualityDirty = false;
  qualityPolicyDirty = false;
  qualityChosen = new Set(
    state.selection?.names ||
      state.report?.images
        .filter((item) => item.decodable)
        .map((item) => item.name) ||
      [],
  );
  if (state.report) {
    $("quality-blur").value = state.report.policy.blur_threshold;
    $("quality-dark").value = state.report.policy.dark_mean;
    $("quality-bright").value = state.report.policy.bright_mean;
    $("quality-near").value = state.report.policy.near_distance;
  }
  renderQuality();
  updateInputReview(state);
}

async function loadQuality(id) {
  const request = ++qualityRequest;
  if ($("input_type").value !== "images") {
    resetQuality();
    return;
  }
  const state = await api(
    "/api/quality?image_set_id=" + encodeURIComponent(id),
  );
  if (request === qualityRequest && id === window.selectedSetId)
    acceptQuality(state);
}

const chooseBeforeQuality = chooseInput;
chooseInput = (item) => {
  chooseBeforeQuality(item);
  resetQuality();
  if (item.input_type === "images")
    return loadQuality(item.id).catch((error) => message(error.message, true));
  return Promise.resolve();
};

$("quality-analyze").onclick = async () => {
  if (qualityBusy) return;
  if ($("input_type").value !== "images")
    return message(
      "请选择原始照片；已有 SfM / Brush 数据不能在这里筛选。",
      true,
    );
  qualityBusy = true;
  $("quality-analyze").disabled = true;
  $("quality-save").disabled = true;
  $("quality-summary").textContent = "正在解码并检测图片，请稍候…";
  const projectId = window.activeProjectId;
  try {
    if (!window.selectedSetId) {
      const item = await api("/api/image-sets", {
        project_id: projectId,
        path: $("input").value,
        input_type: "images",
      });
      if (projectId !== window.activeProjectId) return;
      window.selectedSetId = item.id;
    }
    const id = window.selectedSetId,
      request = ++qualityRequest;
    const state = await api("/api/quality-analyze", {
      image_set_id: id,
      thresholds: {
        blur_threshold: +$("quality-blur").value,
        dark_mean: +$("quality-dark").value,
        bright_mean: +$("quality-bright").value,
        near_distance: +$("quality-near").value,
      },
    });
    if (request !== qualityRequest || id !== window.selectedSetId) return;
    acceptQuality(state);
    await loadProjectDetail();
    message("检测完成。请确认勾选照片并点击“保存筛选”。");
  } catch (error) {
    $("quality-summary").textContent = "检测未完成：" + error.message;
    message(error.message, true);
  } finally {
    qualityBusy = false;
    $("quality-analyze").disabled = false;
    if (qualityState?.report) qualityCount();
  }
};

$("quality-save").onclick = async () => {
  if (!qualityState?.report || qualityBusy || qualityPolicyDirty) return;
  qualityBusy = true;
  $("quality-save").disabled = true;
  const id = qualityState.image_set_id,
    request = qualityRequest;
  try {
    const state = await api("/api/image-selection", {
      image_set_id: id,
      fingerprint: qualityState.report.fingerprint,
      names: [...qualityChosen],
    });
    if (request !== qualityRequest || id !== window.selectedSetId) return;
    acceptQuality(state);
    message(
      "筛选已保存：" + state.selection.names.length + " 张照片，可开始重建。",
    );
  } catch (error) {
    message(error.message, true);
  } finally {
    qualityBusy = false;
    if (qualityState?.report) qualityCount();
  }
};
$("quality-all").onclick = () => {
  qualityChosen = new Set(
    qualityState.report.images
      .filter((item) => item.decodable)
      .map((item) => item.name),
  );
  qualityDirty = true;
  renderQuality();
};
$("quality-dedup").onclick = () => {
  const seen = new Set();
  for (const item of qualityState.report.images) {
    if (!qualityChosen.has(item.name)) continue;
    if (!item.decodable || seen.has(item.sha256))
      qualityChosen.delete(item.name);
    else seen.add(item.sha256);
  }
  qualityDirty = true;
  renderQuality();
};
$("quality-none").onclick = () => {
  qualityChosen.clear();
  qualityDirty = true;
  renderQuality();
};
$("quality-issues").onchange = renderQuality;
for (const id of ["input", "input_type", "project-select", "folder-choose"])
  $(id).addEventListener(
    id === "folder-choose" ? "click" : "change",
    resetQuality,
  );
$("saved-input").addEventListener("change", () => {
  if (!$("saved-input").value) resetQuality();
});
$("method").addEventListener("change", () => {
  if ($("input_type").value !== "images") resetQuality();
});
for (const id of [
  "quality-blur",
  "quality-dark",
  "quality-bright",
  "quality-near",
])
  $(id).addEventListener("input", () => {
    qualityDirty = true;
    qualityPolicyDirty = true;
    $("quality-summary").textContent = "阈值已修改，请重新检测后保存筛选。";
    $("quality-save").disabled = true;
  });
const uploadBeforeQuality = $("upload").onclick;
$("upload").onclick = async () => {
  await uploadBeforeQuality();
  resetQuality();
  if (window.selectedSetId)
    await loadQuality(window.selectedSetId).catch((error) =>
      message(error.message, true),
    );
};
const submitBeforeQuality = $("form").onsubmit;
$("form").onsubmit = async (event) => {
  event.preventDefault();
  if (
    $("input_type").value === "images" &&
    (qualityBusy ||
      qualityDirty ||
      !qualityState?.selection ||
      qualityState.image_set_id !== window.selectedSetId)
  ) {
    message("请先检测当前图片集、确认勾选并保存筛选，再开始重建。", true);
    qualitySection.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  await submitBeforeQuality(event);
};
resetQuality();
