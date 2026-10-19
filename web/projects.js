let projectData = null,
  activeTab = "tasks";
async function loadProjectDetail() {
  const id = window.activeProjectId;
  const data = await api("/api/project?id=" + id);
  if (id !== window.activeProjectId) return;
  projectData = data;
  $("project-title").textContent = data.name;
  $("project-info").textContent =
    data.image_sets.length +
    " 个图片集 · " +
    data.tasks.length +
    " 个任务 · " +
    data.models.length +
    " 个结果文件";
  renderTasks();
  renderImages();
  renderModels();
}
function renderTasks() {
  const search = $("task-search").value.toLowerCase(),
    status = $("task-status").value,
    method = $("task-method").value;
  $("project-tasks").replaceChildren();
  const rows = projectData.tasks.filter(
    (t) =>
      t.name.toLowerCase().includes(search) &&
      (!status || t.status === status) &&
      (!method || t.method === method),
  );
  for (const task of rows) {
    const row = document.createElement("tr");
    cell(
      row,
      button(task.name, () => openTask(task.id), "text-link"),
    );
    cell(row, task.method);
    cell(row, statusBadge(task.status));
    cell(row, new Date(task.created_at).toLocaleString());
    cell(row, fmt(task.elapsed));
    cell(row, task.models.length);
    cell(
      row,
      button(task.status === "completed" ? "查看结果 ↗" : "查看详情", () =>
        task.status === "completed" ? openResult(task) : openTask(task.id),
      ),
    );
    $("project-tasks").append(row);
  }
  if (!rows.length)
    $("project-tasks").innerHTML =
      '<tr><td class="empty" colspan="7">没有符合条件的任务</td></tr>';
}
function renderImages() {
  $("project-inputs").replaceChildren();
  for (const item of projectData.image_sets) {
    const section = document.createElement("div");
    section.className = "image-item";
    const strip = document.createElement("div");
    strip.className = "image-strip";
    for (const image of item.images.slice(0, 3)) {
      const img = document.createElement("img");
      img.src =
        "/api/source-image?set=" +
        item.id +
        "&name=" +
        encodeURIComponent(image.name);
      img.alt = image.name;
      img.loading = "lazy";
      strip.append(img);
    }
    const title = document.createElement("h3");
    title.textContent = item.name;
    const count = document.createElement("p");
    count.textContent = inputCount(item);
    const path = document.createElement("p");
    path.className = "note";
    path.textContent = item.path;
    section.append(
      strip,
      title,
      count,
      path,
      button("用于重建 →", () => {
        location.href = routeURL("reconstruct", { input: item.id });
      }),
    );
    $("project-inputs").append(section);
  }
  if (!projectData.image_sets.length)
    $("project-inputs").innerHTML =
      '<p class="empty">暂无图片集。前往重建工作台上传照片。</p>';
}
function renderModels() {
  $("project-models").replaceChildren();
  for (const task of projectData.tasks.filter((t) => t.models.length)) {
    const group = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent =
      task.name + " · " + task.method + " · " + task.models.length + " 个文件";
    group.append(title);
    const list = document.createElement("div");
    list.className = "panel-body";
    for (const model of task.models) {
      const p = document.createElement("p");
      p.textContent =
        model.kind +
        " · " +
        fmt(model.bytes / 1024 ** 2) +
        " MB · " +
        model.path;
      list.append(p);
    }
    list.append(button("查看任务结果 ↗", () => openResult(task)));
    group.append(list);
    $("project-models").append(group);
  }
  if (!projectData.models.length)
    $("project-models").innerHTML =
      '<p class="empty">完成重建后，可在这里查看模型来源与文件记录。</p>';
}
async function openTask(id) {
  const task = await api("/api/task?id=" + id);
  $("history-title").textContent = task.name;
  $("history-status").textContent =
    (taskStatus[task.status] || task.status) +
    " · " +
    task.method +
    " · " +
    task.output;
  $("history-error").textContent = task.error || "";
  $("history-params").textContent = JSON.stringify(
    { parameters: task.parameters, command: task.command, stages: task.states },
    null,
    2,
  );
  $("history-log").textContent = task.log_text || "暂无运行日志";
  $("history-view").hidden = task.status !== "completed";
  $("history-follow").hidden = task.status === "completed";
  $("history-view").onclick = $("history-follow").onclick = () =>
    openResult(task);
  if (!$("task-dialog").open) $("task-dialog").showModal();
}
$("history-close").onclick = () => $("task-dialog").close();
for (const el of document.querySelectorAll("[data-tab]"))
  el.onclick = () => {
    activeTab = el.dataset.tab;
    for (const tab of ["tasks", "images", "models"])
      $("tab-" + tab).hidden = tab !== activeTab;
    document
      .querySelectorAll("[data-tab]")
      .forEach((t) =>
        t.classList.toggle("active", t.dataset.tab === activeTab),
      );
    history.replaceState({}, "", routeURL("projects", { tab: activeTab }));
  };
for (const id of ["task-search", "task-status", "task-method"])
  $(id).addEventListener("input", () => projectData && renderTasks());
$("project-rename").onclick = async () => {
  const name = prompt("新的项目名称", projectData.name);
  if (name === null) return;
  try {
    await api("/api/project-rename", { id: window.activeProjectId, name });
    location.reload();
  } catch (error) {
    message(error.message, true);
  }
};
initShell("projects", async () => {
  await loadProjectDetail();
  const tab = new URLSearchParams(location.search).get("tab");
  if (["tasks", "images", "models"].includes(tab))
    document.querySelector('[data-tab="' + tab + '"]').click();
}).catch((error) => message(error.message, true));
setInterval(
  () => loadProjectDetail().catch((error) => message(error.message, true)),
  5000,
);
