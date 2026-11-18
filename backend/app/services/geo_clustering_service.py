"""
Geo-Clustering Service
-----------------------
Two related but distinct jobs, both built on DBSCAN with haversine distance
(so "epsilon" is expressed in real metres, not raw lat/lon degrees which
distort distance because degrees of longitude shrink toward the poles):

1. DUPLICATE DETECTION (tight radius, e.g. 50m):
   When a new complaint comes in, cluster it against OPEN issues of the same
   category nearby. If it falls in an existing cluster, link it as a
   duplicate report instead of creating a new ticket — this is what feeds
   the priority engine's "duplicate_count" boost.

2. HOTSPOT DETECTION (wide radius, e.g. 300-500m):
   Periodically cluster ALL issues (any category, any time window) across
   the city to find areas with unusually dense complaint activity — e.g. a
   whole neighbourhood with recurring drainage problems, which signals a
   systemic infrastructure issue rather than N independent complaints.
"""
import numpy as np
from sklearn.cluster import DBSCAN
from dataclasses import dataclass, field

EARTH_RADIUS_M = 6_371_000


@dataclass
class GeoPoint:
    issue_id: str
    lat: float
    lon: float
    category: str = ""


@dataclass
class Cluster:
    cluster_id: int
    points: list = field(default_factory=list)
    center_lat: float = 0.0
    center_lon: float = 0.0

    @property
    def size(self):
        return len(self.points)


def _cluster(points: list[GeoPoint], radius_meters: float, min_samples: int = 2):
    if not points:
        return []

    coords_rad = np.radians([[p.lat, p.lon] for p in points])
    eps_rad = radius_meters / EARTH_RADIUS_M

    db = DBSCAN(eps=eps_rad, min_samples=min_samples, metric="haversine")
    labels = db.fit_predict(coords_rad)

    clusters = {}
    for point, label in zip(points, labels):
        if label == -1:
            continue  # noise point — not part of any cluster
        clusters.setdefault(label, []).append(point)

    result = []
    for label, pts in clusters.items():
        center_lat = float(np.mean([p.lat for p in pts]))
        center_lon = float(np.mean([p.lon for p in pts]))
        result.append(Cluster(cluster_id=int(label), points=pts,
                               center_lat=center_lat, center_lon=center_lon))
    return sorted(result, key=lambda c: c.size, reverse=True)


def find_duplicates(new_point: GeoPoint, open_issues_same_category: list[GeoPoint],
                     radius_meters: float = 50) -> list[str]:
    """Return issue_ids of existing open issues that the new report duplicates."""
    all_points = open_issues_same_category + [new_point]
    clusters = _cluster(all_points, radius_meters=radius_meters, min_samples=1)
    for cluster in clusters:
        ids_in_cluster = [p.issue_id for p in cluster.points]
        if new_point.issue_id in ids_in_cluster and cluster.size > 1:
            return [i for i in ids_in_cluster if i != new_point.issue_id]
    return []


def find_hotspots(all_open_issues: list[GeoPoint], radius_meters: float = 350,
                   min_issues: int = 4) -> list[Cluster]:
    """City-wide clustering to surface areas with abnormally high issue density."""
    return _cluster(all_open_issues, radius_meters=radius_meters, min_samples=min_issues)


if __name__ == "__main__":
    # Sample data: a handful of points scattered across Gurugram, with a
    # deliberate cluster of "same pothole reported by 4 different citizens"
    # near Sector 29, plus a drainage hotspot near Sushant Lok.
    sample_issues = [
        GeoPoint("R1", 28.4601, 77.0648, "Road_Damage"),   # Sector 29 pothole - report 1
        GeoPoint("R2", 28.4603, 77.0651, "Road_Damage"),   # same pothole - report 2 (~35m away)
        GeoPoint("R3", 28.4599, 77.0645, "Road_Damage"),   # same pothole - report 3
        GeoPoint("R4", 28.4700, 77.0900, "Road_Damage"),   # unrelated pothole, far away

        GeoPoint("D1", 28.4650, 77.0730, "Drainage_Sewage"),
        GeoPoint("D2", 28.4655, 77.0735, "Drainage_Sewage"),
        GeoPoint("D3", 28.4648, 77.0725, "Drainage_Sewage"),
        GeoPoint("D4", 28.4652, 77.0740, "Drainage_Sewage"),

        GeoPoint("G1", 28.4550, 77.0500, "Garbage_Waste"),  # isolated, no cluster
    ]

    print("=== Duplicate detection test ===")
    new_report = GeoPoint("R5", 28.4602, 77.0649, "Road_Damage")
    existing_road_issues = [p for p in sample_issues if p.category == "Road_Damage"]
    dupes = find_duplicates(new_report, existing_road_issues)
    print(f"New report R5 duplicates: {dupes}")

    print("\n=== Hotspot detection test (city-wide, min 4 issues within 350m) ===")
    hotspots = find_hotspots(sample_issues, radius_meters=350, min_issues=3)
    for h in hotspots:
        categories = {p.category for p in h.points}
        print(f"Hotspot #{h.cluster_id}: {h.size} issues at "
              f"({h.center_lat:.4f}, {h.center_lon:.4f}) — categories: {categories}")
