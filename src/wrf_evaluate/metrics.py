"""配对样本的连续误差、降水阈值和零度上下事件指标。"""

from dataclasses import dataclass
import math

import numpy as np


class MetricError(ValueError):
    """指标输入范围、形状或阈值无效。"""


@dataclass(frozen=True)
class ContinuousMetrics:
    sample_count: int
    rmse: float | None
    mae: float | None
    bias: float | None
    correlation: float | None


@dataclass(frozen=True)
class EventMetrics:
    threshold: float
    sample_count: int
    hits: int
    misses: int
    false_alarms: int
    correct_negatives: int
    miss_rate: float | None
    false_alarm_rate: float | None
    false_alarm_ratio: float | None


def _paired_values(forecast: np.ndarray, observation: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    forecast = np.ma.asarray(forecast)
    observation = np.ma.asarray(observation)
    if forecast.shape != observation.shape:
        raise MetricError("预报和实况形状必须一致，不能广播配对")
    if forecast.dtype.kind not in "iuf" or observation.dtype.kind not in "iuf":
        raise MetricError("预报和实况必须为实数数组")
    forecast = np.ma.array(forecast, dtype=np.float64, copy=False)
    observation = np.ma.array(observation, dtype=np.float64, copy=False)
    valid = (
        ~np.ma.getmaskarray(forecast)
        & ~np.ma.getmaskarray(observation)
        & np.isfinite(forecast.data)
        & np.isfinite(observation.data)
    )
    return forecast.data[valid], observation.data[valid]


def continuous_metrics(forecast: np.ndarray, observation: np.ndarray) -> ContinuousMetrics:
    """计算 RMSE、MAE、Bias（预报减实况）和皮尔逊相关系数 R。"""
    forecast, observation = _paired_values(forecast, observation)
    if not forecast.size:
        return ContinuousMetrics(0, None, None, None, None)
    try:
        with np.errstate(over="raise", invalid="raise"):
            error = forecast - observation
            correlation = None
            if (forecast.size >= 2
                    and np.any(forecast != forecast[0])
                    and np.any(observation != observation[0])):
                correlation = float(np.corrcoef(forecast, observation)[0, 1])
            return ContinuousMetrics(
                int(error.size),
                float(np.sqrt(np.mean(error * error))),
                float(np.mean(np.abs(error))),
                float(np.mean(error)),
                correlation,
            )
    except FloatingPointError as exc:
        raise MetricError("误差计算溢出，请检查单位、缺测码和输入范围") from exc


def precipitation_event_metrics(
    forecast_mm: np.ndarray, observation_mm: np.ndarray, threshold_mm: float,
) -> EventMetrics:
    """降水量达到阈值即事件；返回漏报率及空报比，比例无分母时为 None。

    漏报率 = misses / (hits + misses)，空报比 = false alarms / (hits + false alarms)。
    """
    if isinstance(threshold_mm, bool) or not isinstance(threshold_mm, (int, float)):
        raise MetricError("降水阈值须为正的有限数值（mm）")
    threshold = float(threshold_mm)
    if not math.isfinite(threshold) or threshold <= 0:
        raise MetricError("降水阈值须为正的有限数值（mm）")
    forecast, observation = _paired_values(forecast_mm, observation_mm)
    if np.any(forecast < 0) or np.any(observation < 0):
        raise MetricError("有效降水量不能为负，请检查缺测码和累计规则")
    forecast_event = forecast >= threshold
    observation_event = observation >= threshold
    return _event_metrics(forecast_event, observation_event, threshold)


def temperature_event_metrics(
    forecast_c: np.ndarray, observation_c: np.ndarray,
) -> tuple[EventMetrics, EventMetrics]:
    """按共同有效样本返回非正温（≤0℃）、正温（>0℃）事件统计。

    不取整，不把缺测值归入任一事件；率值分母为零时返回 None。
    """
    forecast, observation = _paired_values(forecast_c, observation_c)
    non_positive_forecast = forecast <= 0
    non_positive_observation = observation <= 0
    return (
        _event_metrics(non_positive_forecast, non_positive_observation, 0.0),
        _event_metrics(~non_positive_forecast, ~non_positive_observation, 0.0),
    )


def _event_metrics(
    forecast_event: np.ndarray, observation_event: np.ndarray, threshold: float,
) -> EventMetrics:
    """内部共用的布尔列联统计；调用方先过滤缺测并定义事件。"""
    hits = int(np.count_nonzero(forecast_event & observation_event))
    misses = int(np.count_nonzero(~forecast_event & observation_event))
    false_alarms = int(np.count_nonzero(forecast_event & ~observation_event))
    correct_negatives = int(np.count_nonzero(~forecast_event & ~observation_event))
    observed_events = hits + misses
    forecast_events = hits + false_alarms
    non_events = false_alarms + correct_negatives
    return EventMetrics(
        threshold, int(forecast_event.size), hits, misses, false_alarms, correct_negatives,
        misses / observed_events if observed_events else None,
        false_alarms / non_events if non_events else None,
        false_alarms / forecast_events if forecast_events else None,
    )
