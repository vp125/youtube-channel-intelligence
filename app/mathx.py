"""Small, dependency-free statistics helpers (rates, intervals, tests, test planning). Deterministic; no LLM."""
import math


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _z_for(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, accurate to ~1e-9)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def wilson(clicks: float, n: float, conf: float = 0.95) -> tuple[float, float] | None:
    """Wilson score interval for a proportion; sound for small n and for p near 0 or 1."""
    if not n:
        return None
    z = _z_for(1 - (1 - conf) / 2)
    p = clicks / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def two_prop_p(k1: float, n1: float, k2: float, n2: float) -> float | None:
    """Two-sided p-value, pooled two-proportion z-test. None when a sample is empty."""
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k1 / n1 - k2 / n2) / se
    return 2 * (1 - _phi(abs(z)))


def sample_size_two_prop(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int | None:
    """Impressions needed PER VARIANT to detect CTR p1 vs p2 (two-sided z-test)."""
    if p1 == p2 or not (0 < p1 < 1 and 0 < p2 < 1):
        return None
    za, zb = _z_for(1 - alpha / 2), _z_for(power)
    pbar = (p1 + p2) / 2
    n = ((za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / (p1 - p2) ** 2
    return int(math.ceil(n))


def mad_spikes(values: list[float], k: float = 4.0) -> list[int]:
    """Indexes of values far above the typical level (median + k * scaled MAD). Robust to the spikes themselves."""
    if len(values) < 7:
        return []
    s = sorted(values)
    med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    dev = sorted(abs(v - med) for v in values)
    mad = dev[len(dev) // 2] if len(dev) % 2 else (dev[len(dev) // 2 - 1] + dev[len(dev) // 2]) / 2
    scale = max(1.4826 * mad, 1.0)   # floor so near-constant series still need a real jump
    return [i for i, v in enumerate(values) if v > med + k * scale]


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def beta_shrunk_rate(clicks: float, n: float, prior_rate: float, prior_strength: float = 40.0) -> float:
    """Empirical-Bayes style estimate: pulls a small-sample rate toward the channel rate (prior worth `prior_strength` impressions)."""
    return (clicks + prior_rate * prior_strength) / (n + prior_strength) if (n + prior_strength) else prior_rate


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 4 or len(xs) != len(ys):
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None
