"""Calibrated command/airflow mapping; airflow is not electrical power."""


def speed_curve(value):
    points = [tuple(float(v.strip()) for v in item.split(":")) for item in value.split(",")]
    if (len(points) < 2 or any(len(p) != 2 for p in points)
            or points[0] != (0, 0) or points[-1] != (100, 100)
            or any(not 0 <= v <= 100 for p in points for v in p)
            or any(a[0] >= b[0] or a[1] >= b[1] for a, b in zip(points, points[1:]))):
        raise ValueError("Expected increasing command:airflow points from 0:0 to 100:100")
    return points


def interpolate(value, points, inverse=False):
    if inverse:
        points = [(y, x) for x, y in points]
    value = max(0, min(100, value))
    for (x, y), (xx, yy) in zip(points, points[1:]):
        if value <= xx:
            return y + (yy - y) * (value - x) / (xx - x)
    return 100.0
