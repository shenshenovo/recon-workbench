"""跨层共用的异常类型。

放在最底层，避免 ``workspace``（持久化）与 ``access``（授权）互相导入。
"""


class AccessDenied(ValueError):
    """已登录但没有该资源的权限；HTTP 层据此返回 403 而不是 400。

    继承 ValueError，是为了让任何未显式处理的调用路径仍落在既有的参数错误分支里，
    不会因为漏判而把越权请求变成 500。
    """


class RateLimited(ValueError):
    """短时间失败次数超过阈值；HTTP 层返回 429，前端据此提示"稍后再试"。

    与 AccessDenied 分开，避免把"等一会儿"和"没有权限"混成同一个 4xx 语义。
    """
