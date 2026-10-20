"""Small, dependency-free inferential statistics for the AVAT desktop views.

Reference definitions (not the mockup's approximate probability calculations):
https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.chi2_contingency.html
https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.kruskal.html
https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.pearsonr.html
https://dlmf.nist.gov/8.9 and https://dlmf.nist.gov/8.17

The gamma/beta functions use convergent series and modified-Lentz continued
fractions. Pearson's two-sided probability is evaluated using the beta form
of the Student t distribution with n-2 degrees of freedom, not a normal or
chi-square approximation. All results remain exploratory, not causal claims.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean

_EPSILON = 2e-14
_TINY = 1e-300
_ITERATIONS = 10_000


@dataclass(frozen=True, slots=True)
class StatisticalTest:
    method: str
    statistic: float | None = None
    p_value: float | None = None
    effect_size: float | None = None
    df: float | None = None
    n: int = 0
    reason: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.reason and self.statistic is not None and self.p_value is not None


def _probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def gamma_survival(shape: float, value: float) -> float:
    """Return Q(shape, value), the regularized upper incomplete gamma."""
    if shape <= 0 or value < 0 or not math.isfinite(shape) or math.isnan(value):
        raise ValueError("감마 분포의 인수가 올바르지 않습니다.")
    if value == 0:
        return 1.0
    if math.isinf(value):
        return 0.0
    scale = math.exp(shape * math.log(value) - value - math.lgamma(shape))
    if value < shape + 1:
        term = total = 1 / shape
        for index in range(1, _ITERATIONS + 1):
            term *= value / (shape + index)
            total += term
            if abs(term) <= abs(total) * _EPSILON:
                return _probability(1 - scale * total)
    else:
        b = value + 1 - shape
        c = 1 / _TINY
        d = 1 / b
        total = d
        for index in range(1, _ITERATIONS + 1):
            coefficient = -index * (index - shape)
            b += 2
            d = coefficient * d + b
            if abs(d) < _TINY:
                d = _TINY
            c = b + coefficient / c
            if abs(c) < _TINY:
                c = _TINY
            d = 1 / d
            change = d * c
            total *= change
            if abs(change - 1) <= _EPSILON:
                return _probability(scale * total)
    raise ArithmeticError("감마 확률 계산이 수렴하지 않았습니다.")


def _beta_fraction(a: float, b: float, x: float) -> float:
    summed, ap1, am1 = a + b, a + 1, a - 1
    c = 1.0
    d = 1 - summed * x / ap1
    if abs(d) < _TINY:
        d = _TINY
    d = 1 / d
    total = d
    for index in range(1, _ITERATIONS + 1):
        even = 2 * index
        for coefficient in (
            index * (b - index) * x / ((am1 + even) * (a + even)),
            -(a + index) * (summed + index) * x / ((a + even) * (ap1 + even)),
        ):
            d = 1 + coefficient * d
            if abs(d) < _TINY:
                d = _TINY
            c = 1 + coefficient / c
            if abs(c) < _TINY:
                c = _TINY
            d = 1 / d
            change = d * c
            total *= change
        if abs(change - 1) <= _EPSILON:
            return total
    raise ArithmeticError("베타 확률 계산이 수렴하지 않았습니다.")


def beta_cdf(a: float, b: float, x: float) -> float:
    """Return I_x(a,b), with symmetry to avoid subtractive cancellation."""
    if a <= 0 or b <= 0 or not all(math.isfinite(v) for v in (a, b, x)) or not 0 <= x <= 1:
        raise ValueError("베타 분포의 인수가 올바르지 않습니다.")
    if x in (0, 1):
        return x
    scale = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return _probability(scale * _beta_fraction(a, b, x) / a)
    return _probability(1 - scale * _beta_fraction(b, a, 1 - x) / b)


def chi_square(table: list[list[int]], *, minimum_n: int = 20) -> StatisticalTest:
    """Pearson independence test, without Yates continuity correction."""
    method = "chi_square"
    if not table or not table[0]:
        return StatisticalTest(method, reason="비교할 유효 사고가 없습니다.")
    row_totals = [sum(row) for row in table]
    column_totals = [sum(row[index] for row in table) for index in range(len(table[0]))]
    n = sum(row_totals)
    active_rows = [index for index, count in enumerate(row_totals) if count]
    active_columns = [index for index, count in enumerate(column_totals) if count]
    df = (len(active_rows) - 1) * (len(active_columns) - 1)
    if not n or len(active_rows) < 2 or len(active_columns) < 2:
        return StatisticalTest(method, n=n, reason="각 축에 유효 범주가 2개 이상 필요합니다.")
    terms, low, tiny = [], 0, 0
    for row in active_rows:
        for column in active_columns:
            expected = row_totals[row] * column_totals[column] / n
            low += expected < 5
            tiny += expected < 1
            terms.append((table[row][column] - expected) ** 2 / expected)
    statistic = math.fsum(terms)
    effect = math.sqrt(statistic / (n * min(len(active_rows) - 1, len(active_columns) - 1)))
    warnings = []
    cells = len(active_rows) * len(active_columns)
    if low:
        warnings.append(f"기대 빈도 5 미만 {low}/{cells}칸입니다. 검정 결과는 참고용입니다.")
    if tiny or low / cells > 0.2:
        warnings.append("희소한 범주가 많아 근사 p값이 부정확할 수 있습니다.")
    if n < minimum_n:
        return StatisticalTest(method, statistic, effect_size=effect, df=df, n=n,
                               reason=f"유효 사고가 {minimum_n}건 미만이라 검정하지 않았습니다.",
                               warnings=tuple(warnings))
    return StatisticalTest(method, statistic, gamma_survival(df / 2, statistic / 2),
                           effect, df, n, warnings=tuple(warnings))


def kruskal_wallis(samples: list[tuple[float, ...]], *, minimum_group: int = 5) -> StatisticalTest:
    """Rank-sum H with average tied ranks and the exact tie correction factor."""
    eligible = [values for values in samples if len(values) >= minimum_group]
    warnings = () if len(eligible) == len(samples) else (
        f"사고가 {minimum_group}건 미만인 그룹은 검정에서 제외했습니다.",
    )
    n = sum(len(values) for values in eligible)
    if len(eligible) < 2:
        return StatisticalTest("kruskal_wallis", n=n,
                               reason=f"사고가 {minimum_group}건 이상인 그룹이 2개 이상 필요합니다.",
                               warnings=warnings)
    pooled = sorted((value, index) for index, values in enumerate(eligible) for value in values)
    rank_sums = [0.0] * len(eligible)
    tie_sum, start = 0, 0
    while start < n:
        end = start + 1
        while end < n and pooled[end][0] == pooled[start][0]:
            end += 1
        rank = (start + 1 + end) / 2
        tie = end - start
        tie_sum += tie**3 - tie
        for _, index in pooled[start:end]:
            rank_sums[index] += rank
        start = end
    correction = 1 - tie_sum / (n**3 - n)
    if correction <= 0:
        return StatisticalTest("kruskal_wallis", n=n, reason="모든 유효 값이 같아 검정할 수 없습니다.", warnings=warnings)
    h = (12 / (n * (n + 1)) * math.fsum(
        ranks**2 / len(values) for ranks, values in zip(rank_sums, eligible, strict=True)
    ) - 3 * (n + 1)) / correction
    h = max(0.0, h)
    df = len(eligible) - 1
    return StatisticalTest("kruskal_wallis", h, gamma_survival(df / 2, h / 2),
                           df=df, n=n, warnings=warnings)


def pearson(
    xs: tuple[float, ...], ys: tuple[float, ...]
) -> tuple[float | None, float | None, float | None, StatisticalTest]:
    """Return r, OLS slope/intercept, and the two-sided Student-t probability."""
    n = len(xs)
    if n != len(ys):
        raise ValueError("수치 쌍의 개수가 다릅니다.")
    if n < 3:
        return None, None, None, StatisticalTest("pearson", n=n, reason="유효 수치 쌍이 3건 이상 필요합니다.")
    # Normalize before centering so squaring large/small values cannot overflow
    # or underflow. The normalization does not change the correlation.
    scale_x, scale_y = max(abs(value) for value in xs), max(abs(value) for value in ys)
    nx = [value / scale_x if scale_x else 0.0 for value in xs]
    ny = [value / scale_y if scale_y else 0.0 for value in ys]
    mx, my = mean(nx), mean(ny)
    dx, dy = [value - mx for value in nx], [value - my for value in ny]
    xx, yy = math.fsum(value**2 for value in dx), math.fsum(value**2 for value in dy)
    if not xx or not yy:
        return None, None, None, StatisticalTest("pearson", n=n, reason="한 항목의 값이 모두 같아 상관을 계산할 수 없습니다.")
    xy = math.fsum(x * y for x, y in zip(dx, dy, strict=True))
    r = max(-1.0, min(1.0, xy / math.sqrt(xx * yy)))
    slope = (xy / xx) * (scale_y / scale_x)
    intercept = scale_y * (my - (xy / xx) * mx)
    warnings = ["양측 p값은 독립 관측과 이변량 정규성 가정을 전제로 합니다. 상관은 인과를 뜻하지 않습니다."]
    if math.sqrt(xx) < 1e-13 * abs(mx) or math.sqrt(yy) < 1e-13 * abs(my):
        warnings.append("값의 변화가 매우 작아 수치 오차에 민감할 수 있습니다.")
    if not math.isfinite(slope) or not math.isfinite(intercept):
        slope = intercept = None
        warnings.append("추세선의 수치 범위가 너무 커 표시할 수 없습니다.")
    # t² = r²(n-2)/(1-r²), so df/(df+t²) = 1-r².
    p = beta_cdf((n - 2) / 2, 0.5, (1 - abs(r)) * (1 + abs(r)))
    test = StatisticalTest("pearson", r, p, r, n - 2, n, warnings=tuple(warnings))
    return r, slope, intercept, test


def percentile(sorted_values: tuple[float, ...] | list[float], fraction: float) -> float:
    """Inclusive linear percentile, matching the mockup and existing box plot."""
    position = (len(sorted_values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    weight = position - lower
    return (1 - weight) * sorted_values[lower] + weight * sorted_values[upper]
