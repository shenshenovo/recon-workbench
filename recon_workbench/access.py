"""会话解析与资源授权：谁能访问什么，集中在这里判断。

分层约定：
- ``accounts``   只做口令散列、令牌生成和登录限流，不接触数据库与 HTTP；
- ``workspace``  只做持久化，凡是列表查询都接受 ``owner`` 过滤参数；
- 本模块把"当前请求是谁"解释成明确的归属范围，并把越权统一抛成 AccessDenied；
- ``server``     只负责路由、请求解析、文件响应和任务进程控制。

归属只落在 ``projects.owner_id`` 一列：图片集属于项目，任务属于项目，模型属于任务，
因此不需要在四张表上重复存 owner，也不会出现三处不同步。
"""
from http.cookies import CookieError, SimpleCookie

from . import accounts
from .errors import AccessDenied

COOKIE_NAME = 'rw_session'


def read_token(header):
    """从 Cookie 头取会话令牌；格式异常按"未登录"处理，不抛错。"""
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


def session_cookie(token=None):
    """HttpOnly + SameSite=Strict：脚本读不到，跨站请求带不上。

    回环地址走 http，因此不加 Secure，否则浏览器不会回传。
    """
    if not token:
        return '{}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'.format(COOKIE_NAME)
    return '{0}={1}; Path=/; HttpOnly; SameSite=Strict; Max-Age={2}'.format(
        COOKIE_NAME, token, 60 * 60 * 24 * accounts.SESSION_DAYS)


class Access:
    """一次请求的授权上下文，同时是带归属范围的数据视图。

    ``owner`` 为 ``None`` 有两种来源，语义相同：账号体系还没启用（此时全部数据都是
    历史数据），或登录用户恰好没有任何归属记录。HTTP 层在启用账号后先拦截未登录请求，
    因此不会出现"未登录却拿到别人数据"的情况。
    """

    def __init__(self, store, token=None):
        self.store = store
        self.user = store.session_user(token) if token else None
        self.user_count = store.count_users()

    @property
    def signed_in(self):
        return self.user is not None

    @property
    def accounts_enabled(self):
        return self.user_count > 0

    @property
    def owner(self):
        return self.user['id'] if self.user else None

    @property
    def login_required(self):
        """数据库里已有账号时，其余接口都要求登录。"""
        return self.accounts_enabled and not self.signed_in

    # ---- 带归属的读接口 --------------------------------------------------
    def projects(self):
        return self.store.projects(self.owner)

    def project(self, project_id):
        return self.store.project(project_id, self.owner)

    def image_sets(self, project_id):
        return self.store.image_sets(project_id, self.owner)

    def image_set(self, set_id):
        return self.store.image_set(set_id, self.owner)

    def quality(self, set_id):
        return self.store.quality_state(set_id, self.owner)

    def tasks(self, project_id=None):
        return self.store.tasks(project_id, self.owner)

    def task(self, task_id):
        return self.store.task(task_id, self.owner)

    def models(self, task_id=None, project_id=None):
        return self.store.models(task_id=task_id, project_id=project_id, owner=self.owner)

    def model(self, model_id):
        return self.store.model(model_id, self.owner)

    def outputs(self):
        """当前账号名下任务的结果目录，用于校验预览图片与点云路径。"""
        with self.store.connect() as db:
            rows = db.execute('''SELECT t.output FROM tasks t JOIN projects p ON p.id=t.project_id
                WHERE p.owner_id IS ?''', (self.owner,)).fetchall()
        return [self.store.root / row['output'] for row in rows]

    def owns_output(self, folder, *, exact=False):
        folder = folder.resolve()
        if exact:
            return any(folder == candidate for candidate in self.outputs())
        return any(candidate in folder.parents for candidate in self.outputs())


# ---- 注册、登录、退出 ----------------------------------------------------
def limit_key(username):
    return username.strip().lower() if isinstance(username, str) else ''


def sign_up(store, username, password):
    """注册并直接建立会话，返回 (账号, 令牌, 接管的历史项目数)。"""
    user, claimed = store.register(username, password)
    return user, store.create_session(user['id']), claimed


def sign_in(store, username, password, throttle):
    """用户名或口令错误返回 None；连续失败由 throttle 拦截。"""
    key = limit_key(username)
    throttle.check(key)
    user = store.authenticate(username, password)
    if not user:
        throttle.fail(key)
        return None
    throttle.reset(key)
    return user, store.create_session(user['id'])


def sign_out(store, token):
    return bool(store.delete_session(token))
