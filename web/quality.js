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
const qualityPairReasons = {
  exact_duplicate: "文件完全重复",
  low_texture: "纹理不足",
  aspect_ratio: "宽高比差异过大",
  luminance: "亮度差异过大",
};
const qualityThresholdIds = [
  "quality-blur", "quality-dark", "quality-bright", "quality-near", "quality-phash",
];

function updateQualityControls() {
  const report = qualityState?.report,
    selectable = new Set(
      report?.images.filter((item) => item.decodable).map((item) => item.name) || [],
    );
  for (const id of qualityThresholdIds) $(id).disabled = qualityBusy;
  for (const id of ["quality-all", "quality-dedup", "quality-none"])
    $(id).disabled = qualityBusy || !report;
  for (const check of document.querySelectorAll(".quality-select"))
    check.disabled = qualityBusy || !selectable.has(check.dataset.name);
  $("quality-analyze").disabled = qualityBusy;
  $("quality-save").disabled = qualityBusy || qualityPolicyDirty || !report;
}

function qualityCheckbox(item) {
  const check = document.createElement("input");
  check.type = "checkbox";
  check.className = "quality-select";
  check.dataset.name = item.name;
  check.setAttribute("aria-label", "使用照片 " + item.name);
  check.checked = qualityChosen.has(item.name);
  check.disabled = qualityBusy || !item.decodable;
  check.onchange = () => {
    check.checked
      ? qualityChosen.add(item.name)
      : qualityChosen.delete(item.name);
    for (const other of document.querySelectorAll(".quality-select"))
      if (other.dataset.name === item.name) other.checked = check.checked;
    qualityDirty = true;
    qualityCount();
  };
  return check;
}

function qualityImage(name, className) {
  const img = document.createElement("img");
  img.className = className;
  img.loading = "lazy";
  img.alt = name;
  img.src =
    "/api/source-image?set=" +
    encodeURIComponent(qualityState.image_set_id) +
    "&name=" +
    encodeURIComponent(name) +
    "&preview=1";
  return img;
}

function qualityPairImages(pair, images) {
  const cell = document.createElement("td"),
    group = document.createElement("div");
  group.className = "quality-pair-images";
  for (const name of [pair.a, pair.b]) {
    const item = images.get(name),
      figure = document.createElement("div"),
      label = document.createElement("label"),
      text = document.createElement("span");
    if (item?.decodable)
      figure.append(qualityImage(name, "quality-pair-image"));
    if (item) label.append(qualityCheckbox(item));
    text.textContent = name;
    label.append(text);
    figure.append(label);
    group.append(figure);
  }
  cell.append(group);
  return cell;
}

function qualityPairDistance(distance, threshold, matched, eligible) {
  const cell = document.createElement("td"),
    value = document.createElement("div"),
    result = document.createElement("span");
  value.textContent = (distance ?? "—") + " / " + (threshold ?? "—");
  result.className = "badge" + (matched ? " completed" : "");
  result.textContent =
    distance == null
      ? "未计算"
      : !eligible
        ? "不参与判定"
        : matched
          ? "距离命中"
          : "距离未命中";
  cell.append(value, result);
  return cell;
}

function qualityPairRow(pair, images, thresholds) {
  const row = document.createElement("tr"),
    eligible = document.createElement("td"),
    decision = document.createElement("td");
  row.append(qualityPairImages(pair, images));
  eligible.textContent = pair.eligible
    ? "符合"
    : (pair.exclusion_reasons || [])
        .map((reason) => qualityPairReasons[reason] || reason)
        .join("；") || "不符合";
  row.append(eligible);
  row.append(
    qualityPairDistance(
      pair.dhash_distance,
      thresholds.dhash,
      pair.dhash_match,
      pair.eligible,
    ),
    qualityPairDistance(
      pair.phash_distance,
      thresholds.phash,
      pair.phash_match,
      pair.eligible,
    ),
  );
  decision.textContent = pair.match
    ? "疑似相似"
    : pair.eligible
      ? "未命中"
      : "不参与相似判定";
  if (pair.match) decision.className = "quality-pair-match";
  row.append(decision);
  return row;
}

function qualityPairSummary(report) {
  const c = report.counts,
    sampling = report.pair_sampling || {},
    shown = report.pairs.length;
  return (
    "已比较 " +
    (c.compared_pairs ?? shown) +
    " 对，共同条件符合 " +
    (c.eligible_pairs ?? "—") +
    " 对；dHash 命中 " +
    (c.dhash_pairs ?? "—") +
    " 对，pHash 命中 " +
    (c.phash_pairs ?? "—") +
    " 对，联合命中 " +
    (c.near_duplicate_pairs ?? "—") +
    " 对。展示 " +
    shown +
    " 对" +
    (sampling.truncated
      ? "（上限 " + sampling.display_limit + " 对，命中优先；不是随机抽样）"
      : "（全部可比较图片对）") +
    "。距离命中不代表人工真值；共同条件不符的图片对不参与最终判定。"
  );
}

function renderQualityPairs(report) {
  const legacy = !Array.isArray(report.pairs);
  $("quality-pair-tools").hidden = legacy;
  $("quality-pair-summary").textContent = legacy
    ? "此历史报告仅记录逐图的首个 dHash 匹配，原有结果保留在图片明细中。"
    : qualityPairSummary(report);
  $("quality-pair-rows").replaceChildren();
  if (legacy) {
    $("quality-pair-table").hidden = true;
    $("quality-pair-empty").hidden = true;
    return;
  }
  const filter = $("quality-pair-filter").value,
    pairs = report.pairs.filter((pair) =>
      filter === "matches"
        ? pair.match
        : filter === "disagree"
          ? pair.eligible && pair.dhash_match !== pair.phash_match
          : filter === "excluded"
            ? !pair.eligible
            : true,
    ),
    images = new Map(report.images.map((image) => [image.name, image])),
    thresholds = {
      dhash: report.algorithms?.dhash?.threshold ?? report.policy.near_distance,
      phash: report.algorithms?.phash?.threshold ?? report.policy.phash_distance,
    };
  $("quality-pair-table").hidden = !pairs.length;
  $("quality-pair-empty").hidden = !!pairs.length;
  $("quality-pair-empty").textContent = report.pairs.length
    ? "已展示的图片对中没有符合此条件的记录。"
    : "没有可展示的图片对。";
  for (const pair of pairs)
    $("quality-pair-rows").append(qualityPairRow(pair, images, thresholds));
}

function resetQuality() {
  qualityRequest++;
  qualityState = null;
  qualityChosen = new Set();
  qualityDirty = false;
  qualityPolicyDirty = false;
  window.inputDraftCount = null;
  renderInputSummary();
  $("quality-results").hidden = true;
  updateQualityControls();
  $("quality-summary").classList.toggle("quality-review-required", false);
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
    (qualityPolicyDirty
      ? "需要重新检测，当前筛选不能保存"
      : qualityDirty
        ? "尚未保存，保存后才能开始重建"
        : qualityState.selection
          ? "筛选已保存，重建将使用独立副本"
          : "请确认并保存筛选");
  updateQualityControls();
}

function renderQuality() {
  const report = qualityState?.report;
  $("quality-results").hidden = !report;
  $("quality-summary").classList.toggle(
    "quality-review-required",
    qualityPolicyDirty,
  );
  if (!report) {
    updateQualityControls();
    $("quality-summary").textContent =
      "尚未检测。点击“检测当前图片集”，完成后勾选要使用的照片。";
    return;
  }
  const c = report.counts,
    notice = qualityState.requires_reanalysis
      ? "检测规则或依赖版本已变化，请重新检测后保存筛选；以下仅为历史结果。"
      : qualityPolicyDirty
        ? "阈值已修改，请重新检测后保存筛选；以下为修改前的结果。"
        : "";
  $("quality-summary").textContent =
    notice +
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
      use = document.createElement("td");
    use.append(qualityCheckbox(item));
    row.append(use);
    const preview = document.createElement("td");
    if (item.decodable)
      preview.append(qualityImage(item.name, "quality-image"));
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
          "；疑似相似于 " +
            item.similar_to +
            "，dHash 距离 " +
            item.hash_distance +
            (item.phash_distance == null
              ? ""
              : "，pHash 距离 " + item.phash_distance),
        ),
      );
    if (item.error) flags.append(document.createTextNode("；" + item.error));
    row.append(flags);
    $("quality-rows").append(row);
  }
  renderQualityPairs(report);
  qualityCount();
}

function sameQualityNames(left, right) {
  const a = new Set(left),
    b = new Set(right);
  return a.size === b.size && [...a].every((name) => b.has(name));
}

function acceptQuality(state, preservedNames = null) {
  qualityState = state;
  qualityDirty = false;
  qualityPolicyDirty = !!state.requires_reanalysis;
  const selectable =
    state.report?.images
      .filter((item) => item.decodable)
      .map((item) => item.name) || [];
  if (preservedNames !== null) {
    qualityChosen = new Set(
      selectable.filter((name) => preservedNames.has(name)),
    );
    qualityDirty = !sameQualityNames(
      [...qualityChosen],
      state.selection?.names || [],
    );
  } else {
    qualityChosen = new Set(state.selection?.names || selectable);
  }
  if (state.report) {
    $("quality-blur").value = state.report.policy.blur_threshold;
    $("quality-dark").value = state.report.policy.dark_mean;
    $("quality-bright").value = state.report.policy.bright_mean;
    $("quality-near").value = state.report.policy.near_distance;
    $("quality-phash").value = state.report.policy.phash_distance ?? 6;
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
  updateQualityControls();
  $("quality-summary").textContent = "正在解码并检测图片，请稍候…";
  const projectId = window.activeProjectId,
    request = ++qualityRequest;
  try {
    if (!window.selectedSetId) {
      const item = await api("/api/image-sets", {
        project_id: projectId,
        path: $("input").value,
        input_type: "images",
      });
      if (request !== qualityRequest || projectId !== window.activeProjectId) return;
      window.selectedSetId = item.id;
    }
    const id = window.selectedSetId;
    const state = await api("/api/quality-analyze", {
      image_set_id: id,
      thresholds: {
        blur_threshold: +$("quality-blur").value,
        dark_mean: +$("quality-dark").value,
        bright_mean: +$("quality-bright").value,
        near_distance: +$("quality-near").value,
        phash_distance: +$("quality-phash").value,
      },
    });
    if (request !== qualityRequest || id !== window.selectedSetId) return;
    const preservedNames = qualityState?.report
      ? new Set(qualityChosen)
      : null;
    acceptQuality(state, preservedNames);
    await loadProjectDetail();
    if (request !== qualityRequest || id !== window.selectedSetId) return;
    message("检测完成。请确认勾选照片并点击“保存筛选”。");
  } catch (error) {
    if (request !== qualityRequest) return;
    $("quality-summary").textContent = "检测未完成：" + error.message;
    message(error.message, true);
  } finally {
    qualityBusy = false;
    if (qualityState?.report) qualityCount();
    else updateQualityControls();
  }
};

$("quality-save").onclick = async () => {
  if (!qualityState?.report || qualityBusy || qualityPolicyDirty) return;
  qualityBusy = true;
  updateQualityControls();
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
    if (request !== qualityRequest || id !== window.selectedSetId) return;
    message(error.message, true);
  } finally {
    qualityBusy = false;
    if (qualityState?.report) qualityCount();
    else updateQualityControls();
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
$("quality-pair-filter").onchange = () => {
  if (qualityState?.report) renderQualityPairs(qualityState.report);
};
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
for (const id of qualityThresholdIds)
  $(id).addEventListener("input", () => {
    qualityDirty = true;
    qualityPolicyDirty = true;
    $("quality-summary").textContent = "阈值已修改，请重新检测后保存筛选。";
    $("quality-summary").classList.toggle("quality-review-required", true);
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
