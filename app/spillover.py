"""Generalized Diebold-Yilmaz connectedness for index returns.

The implementation uses the Pesaran-Shin generalized forecast error variance
decomposition (GFEVD), not statsmodels' Cholesky-orthogonalized FEVD. Results
are therefore invariant to the ordering of series, subject to numerical noise.
Connectedness describes shock contribution, not structural causality.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app import correlation as corr


def _display_edges(edges: list[dict], per_source: int = 2) -> list[dict]:
    """Keep the strongest outgoing paths per source for readable visualization.

    The full edge set and FEVD matrix remain in the API response.  This is only
    a deterministic presentation subset, not an analytical threshold.
    """
    per_source = max(1, min(per_source, 5))
    grouped: dict[str, list[dict]] = {}
    for edge in edges:
        grouped.setdefault(edge["source"], []).append(edge)
    selected = []
    for source_edges in grouped.values():
        selected.extend(sorted(
            source_edges,
            key=lambda edge: (-edge["value"], edge["target"]),
        )[:per_source])
    return sorted(
        selected,
        key=lambda edge: (-edge["value"], edge["source"], edge["target"]),
    )


def _align_returns(series: dict[str, list]) -> tuple[list, pd.DataFrame]:
    dates, aligned = corr._align(series)
    frame = pd.DataFrame(aligned, index=dates)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    return list(frame.index), frame


def _generalized_fevd(result, horizon: int) -> np.ndarray:
    """Pesaran-Shin GFEVD normalized to unit row sums."""
    sigma = np.asarray(result.sigma_u, dtype=float)
    coefficients = np.asarray(result.ma_rep(maxn=horizon - 1), dtype=float)[:horizon]
    variables = sigma.shape[0]
    decomposition = np.zeros((variables, variables), dtype=float)
    for affected in range(variables):
        denominator = 0.0
        for phi in coefficients:
            row = phi[affected, :]
            denominator += float(row @ sigma @ row.T)
        if denominator <= 0:
            continue
        for shock in range(variables):
            variance = float(sigma[shock, shock])
            if variance <= 0:
                continue
            numerator = 0.0
            for phi in coefficients:
                impact = float(phi[affected, :] @ sigma[:, shock])
                numerator += impact * impact
            decomposition[affected, shock] = numerator / (variance * denominator)
    row_sums = decomposition.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return decomposition / row_sums


def spillover_network(
    series: dict[str, list],
    maxlags: int = 2,
    horizon: int = 10,
) -> dict:
    names = list(series)
    if len(names) < 2:
        return {"error": "need at least two index series"}
    if not 1 <= maxlags <= 10:
        return {"error": "maxlags must be between 1 and 10"}
    if not 1 <= horizon <= 50:
        return {"error": "horizon must be between 1 and 50"}
    dates, frame = _align_returns(series)
    minimum = max(80, len(names) * maxlags * 3)
    if len(frame) < minimum:
        return {
            "error": (
                f"not enough common data: {len(frame)} observations; "
                f"need at least {minimum}"
            )
        }

    try:
        from statsmodels.tsa.api import VAR

        result = VAR(frame.to_numpy()).fit(maxlags=maxlags, trend="c")
        if not result.is_stable():
            return {"error": "fitted VAR is unstable; connectedness is not reported"}
        decomposition = _generalized_fevd(result, horizon)
    except (ValueError, np.linalg.LinAlgError) as exc:
        return {"error": f"VAR estimation failed: {exc}"}

    diagonal = np.diag(decomposition)
    to_others = decomposition.sum(axis=0) - diagonal
    from_others = decomposition.sum(axis=1) - diagonal
    net = to_others - from_others
    total = float(from_others.sum() / len(names))

    nodes = [
        {
            "name": name,
            "to": round(float(to_others[index]), 4),
            "from": round(float(from_others[index]), 4),
            "net": round(float(net[index]), 4),
        }
        for index, name in enumerate(names)
    ]
    edges = []
    for affected in range(len(names)):
        for shock in range(len(names)):
            if affected == shock:
                continue
            value = float(decomposition[affected, shock])
            if value >= 0.02:
                edges.append({
                    "source": names[shock],
                    "target": names[affected],
                    "value": round(value, 4),
                })

    return {
        "names": names,
        "nodes": nodes,
        "edges": edges,
        "display_edges": _display_edges(edges, per_source=2),
        "display_edge_policy": {
            "type": "top_outgoing_per_source",
            "per_source": 2,
            "full_edge_count": len(edges),
        },
        "matrix": np.round(decomposition, 4).tolist(),
        "total_connectedness": round(total, 4),
        "maxlags": maxlags,
        "horizon": horizon,
        "observations": len(frame),
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "method": "Pesaran-Shin generalized FEVD / Diebold-Yilmaz connectedness",
        "causality_warning": "방향성 연결성은 구조적 인과관계의 증거가 아닙니다.",
    }
