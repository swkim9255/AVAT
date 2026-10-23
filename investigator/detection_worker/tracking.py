"""샘플링한 프레임 사이에서 같은 객체를 잇는 단순 IoU 추적기.

구조물은 정지해 있지만 블랙박스 카메라가 움직이므로 화면 위치가 변한다.
샘플 간격이 넓을수록 IoU가 낮아지므로, IoU와 중심점 거리를 함께 본다.
AVAT 본체의 추적기로 다시 연결해도 되며, 이 결과는 초기 Track 제안이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .postprocess import box_iou


@dataclass
class _Track:
    track_id: int
    class_key: str
    bbox: tuple[float, float, float, float]
    last_sample: int


class SimpleTracker:
    def __init__(self, iou_threshold: float = 0.2, center_ratio: float = 0.6, max_gap: int = 3):
        self.iou_threshold = iou_threshold
        self.center_ratio = center_ratio
        self.max_gap = max_gap
        self._tracks: list[_Track] = []
        self._next_id = 1

    def _affinity(self, track: _Track, bbox: tuple[float, float, float, float]) -> float:
        iou = box_iou(track.bbox, bbox)
        if iou >= self.iou_threshold:
            return 1.0 + iou
        tx, ty, tw, th = track.bbox
        bx, by, bw, bh = bbox
        dx = (tx + tw / 2) - (bx + bw / 2)
        dy = (ty + th / 2) - (by + bh / 2)
        dist = (dx * dx + dy * dy) ** 0.5
        scale = max((tw * tw + th * th) ** 0.5, (bw * bw + bh * bh) ** 0.5, 1.0)
        size_ratio = min(tw * th, bw * bh) / max(tw * th, bw * bh, 1.0)
        if dist <= self.center_ratio * scale and size_ratio >= 0.3:
            return 1.0 - dist / (self.center_ratio * scale) * 0.5
        return 0.0

    def update(
        self, sample_index: int, items: list[tuple[str, tuple[float, float, float, float]]]
    ) -> list[int]:
        """Match (model coarse-class key, bbox), not prompt/subtype labels.

        The runner supplies the taxonomy key (e.g. ``vehicle`` for car, van,
        bus and truck). Geometry, assignment and max-gap rules are unchanged.
        Explicit legacy/custom configs still use their own supplied keys.
        """
        self._tracks = [t for t in self._tracks if sample_index - t.last_sample <= self.max_gap]
        candidates = []
        for di, (cls, bbox) in enumerate(items):
            for ti, track in enumerate(self._tracks):
                if track.class_key != cls:
                    continue
                score = self._affinity(track, bbox)
                if score > 0:
                    candidates.append((score, di, ti))
        assigned: dict[int, int] = {}
        used_tracks: set[int] = set()
        for _score, di, ti in sorted(candidates, reverse=True):
            if di in assigned or ti in used_tracks:
                continue
            assigned[di] = ti
            used_tracks.add(ti)
        ids: list[int] = []
        for di, (cls, bbox) in enumerate(items):
            if di in assigned:
                track = self._tracks[assigned[di]]
                track.bbox = bbox
                track.last_sample = sample_index
            else:
                track = _Track(self._next_id, cls, bbox, sample_index)
                self._next_id += 1
                self._tracks.append(track)
            ids.append(track.track_id)
        return ids
