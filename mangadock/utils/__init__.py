# utils package
import math


def safe_int(value, default=0, minimum=0):
    """宽松解析整数入参：非数字/None/容器回落 default，并把结果夹到 >= minimum。

    2026-09-20 code review P2：请求入参（JSON body / form / query）如果直接
    `int(...)`，恶意或异常值会抛 ValueError 变成 500；负数还会被原样落库。

    2026-09-20 code review 复核补充：Python 的 json 解析器默认接受 `Infinity`
    / `-Infinity` / `1e999` 这类字面量（解析成 float inf），而 `int(inf)` 抛的
    是 OverflowError —— 不在 (TypeError, ValueError) 里，会穿透成 500。
    所以这里显式拒绝非有限浮点，并把 OverflowError 一并吞掉。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= minimum else minimum
