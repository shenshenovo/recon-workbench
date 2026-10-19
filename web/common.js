const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
window.activeProjectId =
  params.get("project") ||
  localStorage.getItem("workbench-project") ||
  "default";
window.selectedSetId = null;
const taskStatus = {
  starting: "正在启动",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  idle: "等待任务",
};
async function api(path, payload) {
  const response = await fetch(
    path,
    payload === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Local-Token": window.LOCAL_TOKEN,
          },
          body: JSON.stringify(payload),
        },
  );
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "请求失败");
  return data;
}
let toastTimer;
function message(text, error = false) {
  $("message").textContent = text;
  $("message").className = "message" + (error ? " error" : "");
  $("message").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("message").hidden = true), 7000);
}
function fmt(value, digits = 2) {
  return value == null
    ? "—"
    : typeof value === "number"
      ? value.toFixed(digits)
      : String(value);
}
function button(text, action, kind = "") {
  const el = document.createElement("button");
  el.type = "button";
  el.className = kind;
  el.textContent = text;
  el.onclick = () =>
    Promise.resolve(action()).catch((error) => message(error.message, true));
  return el;
}
function cell(row, value) {
  const td = document.createElement("td");
  if (value instanceof Node) td.append(value);
  else td.textContent = value ?? "—";
  row.append(td);
  return td;
}
function statusBadge(status) {
  const el = document.createElement("span");
  el.className = "badge " + status;
  el.textContent = taskStatus[status] || status;
  return el;
}
function inputCount(item) {
  if (item.input_type === "sfm-output") return "已有 SfM 结果";
  if (item.input_type !== "images") return "数据集 · " + item.count + " 张";
  if (item.selected_count != null)
    return "已选 " + item.selected_count + " / 总计 " + item.count + " 张";
  return (
    "总计 " +
    item.count +
    " 张 · " +
    (item.selection_status === "pending" ? "待确认筛选" : "未检测")
  );
}
function routeURL(page, extra = {}) {
  const query = new URLSearchParams({
    project: window.activeProjectId,
    ...extra,
  });
  return "/" + page + "?" + query;
}
function syncNav(page) {
  document.querySelectorAll("[data-page]").forEach((link) => {
    link.href = routeURL(link.dataset.page);
    link.classList.toggle("active", link.dataset.page === page);
  });
}
function openResult(task) {
  location.href = routeURL("reconstruct", { task: task.id });
}
function renderGlobalJob(job) {
  const link = $("global-job");
  link.hidden = job.status !== "running";
  if (job.status === "running" && job.task_id) {
    link.href = routeURL("reconstruct", { task: job.task_id });
    link.textContent = "● 任务运行中 · 返回查看";
  }
}
async function globalJob() {
  try {
    renderGlobalJob(await api("/api/job"));
  } catch {}
}
async function initShell(page, onProjectChange) {
  const projects = await api("/api/projects");
  if (!projects.some((p) => p.id === window.activeProjectId))
    window.activeProjectId = "default";
  $("project-select").replaceChildren(
    ...projects.map((p) => new Option(p.name, p.id)),
  );
  $("project-select").value = window.activeProjectId;
  localStorage.setItem("workbench-project", window.activeProjectId);
  syncNav(page);
  $("project-select").onchange = async () => {
    if (window.beforeProjectChange && !window.beforeProjectChange()) {
      $("project-select").value = window.activeProjectId;
      return;
    }
    window.activeProjectId = $("project-select").value;
    window.selectedSetId = null;
    localStorage.setItem("workbench-project", window.activeProjectId);
    history.replaceState({}, "", routeURL(page));
    syncNav(page);
    await onProjectChange();
  };
  $("project-create").onclick = () => {
    $("new-project-name").value = "";
    $("project-dialog").showModal();
  };
  $("project-close").onclick = () => $("project-dialog").close();
  $("new-project-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      if (window.beforeProjectChange && !window.beforeProjectChange()) return;
      const project = await api("/api/projects", {
        name: $("new-project-name").value,
      });
      $("project-dialog").close();
      location.href = "/" + page + "?project=" + project.id;
    } catch (error) {
      message(error.message, true);
    }
  };
  await onProjectChange();
  await globalJob();
  setInterval(globalJob, 3000);
}
function chart(id, points, key) {
  const canvas = $(id),
    ctx = canvas.getContext("2d"),
    w = canvas.width,
    h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.font = "13px system-ui";
  ctx.fillStyle = "#667085";
  if (!points.length) {
    ctx.fillText("暂无留出视图评估数据", 30, 80);
    return;
  }
  const xs = points.map((p) => p.step),
    ys = points.map((p) => p[key]),
    xmin = Math.min(...xs),
    xmax = Math.max(...xs),
    ymin = Math.min(...ys),
    ymax = Math.max(...ys),
    xp = (x) => 60 + ((x - xmin) / (xmax - xmin || 1)) * (w - 85),
    yp = (y) => h - 38 - ((y - ymin) / (ymax - ymin || 1)) * (h - 65);
  ctx.strokeStyle = "#dce2e8";
  ctx.beginPath();
  ctx.moveTo(60, 15);
  ctx.lineTo(60, h - 38);
  ctx.lineTo(w - 20, h - 38);
  ctx.stroke();
  ctx.fillText(ymax.toFixed(2), 5, 22);
  ctx.fillText(ymin.toFixed(2), 5, h - 38);
  ctx.fillText(xmin, 60, h - 10);
  ctx.fillText(xmax, w - 70, h - 10);
  ctx.strokeStyle = "#087f6d";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) =>
    i ? ctx.lineTo(xp(p.step), yp(p[key])) : ctx.moveTo(xp(p.step), yp(p[key])),
  );
  ctx.stroke();
  ctx.fillStyle = "#087f6d";
  for (const p of points) {
    ctx.beginPath();
    ctx.arc(xp(p.step), yp(p[key]), 3, 0, Math.PI * 2);
    ctx.fill();
  }
}
