"""Technical indicators — pure numpy, no external TA library.

Reimplemented from standard formulas (not copied from any repo) so the math is
auditable and dependency-light. Every function takes a 1-D sequence of closing
prices (oldest first) and returns either a full series (numpy array, NaN-padded
at the front where the window isn't filled yet) or a single latest scalar via the
``*_last`` helpers.

These are deterministic and side-effect free, which makes them the unit-test
anchor for the whole trading layer.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "roc",
    "streak",
    "compute_all",
]


def _as_array(values) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise ValueError("indicator input must be 1-D")
    return arr


def sma(values, period: int) -> np.ndarray:
    """Simple moving average. Returns a series; first ``period-1`` entries NaN."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if period <= 0 or arr.size < period:
        return out
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    out[period - 1 :] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def ema(values, period: int) -> np.ndarray:
    """Exponential moving average (standard 2/(n+1) smoothing).

    Seeded with the SMA of the first ``period`` samples; earlier entries NaN.
    """
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if period <= 0 or arr.size < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = arr[:period].mean()
    out[period - 1] = seed
    prev = seed
    for i in range(period, arr.size):
        prev = alpha * arr[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rsi(values, period: int = 14) -> np.ndarray:
    """Wilder's Relative Strength Index. Series; first ``period`` entries NaN."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if arr.size <= period:
        return out
    delta = np.diff(arr)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    # First average over the initial `period` deltas.
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    def _rsi_value(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, arr.size):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def macd(
    values, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD line, signal line, histogram. Each is a series aligned to ``values``."""
    arr = _as_array(values)
    ema_fast = ema(arr, fast)
    ema_slow = ema(arr, slow)
    macd_line = ema_fast - ema_slow
    # Signal = EMA of the MACD line over the valid (non-NaN) region.
    valid = ~np.isnan(macd_line)
    signal_line = np.full(arr.shape, np.nan)
    if valid.sum() >= signal:
        idx = np.where(valid)[0]
        sig_valid = ema(macd_line[idx], signal)
        signal_line[idx] = sig_valid
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(
    values, period: int = 20, num_std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands: (upper, middle, lower). Middle is the SMA."""
    arr = _as_array(values)
    middle = sma(arr, period)
    std = np.full(arr.shape, np.nan)
    for i in range(period - 1, arr.size):
        std[i] = arr[i - period + 1 : i + 1].std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def roc(values, period: int = 10) -> np.ndarray:
    """Rate of change (percent) over ``period`` bars."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if arr.size <= period:
        return out
    past = arr[:-period]
    out[period:] = np.where(past != 0, (arr[period:] - past) / past * 100.0, np.nan)
    return out


def streak(values) -> int:
    """Consecutive up (+) or down (-) closes ending at the latest bar.

    Returns a signed integer: +3 = three higher closes in a row, -2 = two lower
    closes in a row, 0 if the last move was flat or there's not enough data.
    """
    arr = _as_array(values)
    if arr.size < 2:
        return 0
    diffs = np.diff(arr)
    last = diffs[-1]
    if last == 0:
        return 0
    sign = 1 if last > 0 else -1
    count = 0
    for d in diffs[::-1]:
        if (d > 0 and sign > 0) or (d < 0 and sign < 0):
            count += 1
        else:
            break
    return sign * count


def _last(series: np.ndarray) -> float | None:
    """Latest non-NaN value of a series, or None."""
    if series.size == 0:
        return None
    val = series[-1]
    if np.isnan(val):
        return None
    return float(val)


def compute_all(closes) -> dict[str, float | int | None]:
    """Latest-value snapshot of every indicator, for a quote/indicator table.

    Keys mirror the watchlist ``watch_indicators`` naming so threshold checks can
    look an indicator up by name directly.
    """
    arr = _as_array(closes)
    macd_line, signal_line, hist = macd(arr)
    upper, middle, lower = bollinger(arr)
    last_close = float(arr[-1]) if arr.size else None
    sma50 = _last(sma(arr, 50))
    return {
        "price": last_close,
        "sma_7": _last(sma(arr, 7)),
        "sma_20": _last(sma(arr, 20)),
        "sma_50": sma50,
        "sma_200": _last(sma(arr, 200)),
        "ema_12": _last(ema(arr, 12)),
        "ema_26": _last(ema(arr, 26)),
        "rsi_14": _last(rsi(arr, 14)),
        "macd": _last(macd_line),
        "macd_signal": _last(signal_line),
        "macd_hist": _last(hist),
        "bb_upper": _last(upper),
        "bb_middle": _last(middle),
        "bb_lower": _last(lower),
        "roc_10": _last(roc(arr, 10)),
        "streak": streak(arr),
        # convenience derived signal used by some watchlist triggers
        "price_vs_sma50": (
            None if (last_close is None or sma50 is None) else round(last_close - sma50, 6)
        ),
    }
