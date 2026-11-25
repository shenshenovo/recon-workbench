const $ = (id) => document.getElementById(id);
const query = new URLSearchParams(location.search);
const next = () => {
  const value = query.get("next") || "";
  const safe = value.startsWith("/") && !value.startsWith("//");
  return safe && value !== "/login" ? value : "/reconstruct";
};
let mode = "login";

function setMessage(text, error = true) {
  $("auth-message").textContent = text || "";
  $("auth-message").className = error ? "danger" : "note";
}

function setMode(value) {
  mode = value;
  document
    .querySelectorAll("[data-mode]")
    .forEach((el) => el.classList.toggle("active", el.dataset.mode === mode));
  const register = mode === "register";
  $("password2").hidden = !register;
  $("password2").required = register;
  $("password2-label").hidden = !register;
  $("password").autocomplete = register ? "new-password" : "current-password";
  $("submit").textContent = register ? "注册并登录" : "登录";
  setMessage("");
}

async function post(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Local-Token": window.LOCAL_TOKEN,
    },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "请求失败");
  return data;
}

async function bootstrap() {
  try {
    const state = await (await fetch("/api/auth/me")).json();
    if (state.user) {
      location.replace(next());
      return;
    }
    $("auth-hint").textContent = state.user_count
      ? "已注册 " + state.user_count + " 个账号。忘记密码时需要直接修改本机数据库，暂不支持自助重置。"
      : "数据库中还没有账号。首个注册的账号会接管现有的「默认项目 / 历史实验」与全部历史任务；之后注册的账号各自独立。";
  } catch (error) {
    setMessage(error.message);
  }
}

for (const el of document.querySelectorAll("[data-mode]"))
  el.onclick = () => setMode(el.dataset.mode);

$("auth-form").onsubmit = async (event) => {
  event.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  if (mode === "register" && password !== $("password2").value)
    return setMessage("两次输入的密码不一致。");
  $("submit").disabled = true;
  try {
    const data = await post(
      mode === "register" ? "/api/auth/register" : "/api/auth/login",
      { username, password },
    );
    location.replace(next());
    return data;
  } catch (error) {
    setMessage(error.message);
  } finally {
    $("submit").disabled = false;
  }
};

bootstrap();
