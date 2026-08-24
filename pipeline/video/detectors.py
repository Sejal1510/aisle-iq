from __future__ import annotations

from dataclasses import dataclass

PERSON_CLASS_ID = 0


@dataclass(frozen=True)
class Detection:
    bbox_xyxy: tuple[float, float, float, float]
    confidence: float
    class_id: int = PERSON_CLASS_ID


class YoloPersonDetector:
    """Small YOLOv8 adapter for standalone image/frame detection."""

    def __init__(self, model_name: str = "yolov8n.pt", confidence_threshold: float = 0.35):
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "Ultralytics is required for video detection. Install dependencies from requirements.txt."
            ) from exc

        self.model = YOLO(model_name)
        self.confidence_threshold = confidence_threshold

    def detect(self, frame) -> list[Detection]:
        results = self.model.predict(
            frame,
            classes=[PERSON_CLASS_ID],
            conf=self.confidence_threshold,
            verbose=False,
        )
        detections: list[Detection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                xyxy = tuple(float(value) for value in box.xyxy[0].tolist())
                confidence = float(box.conf[0])
                class_id = int(box.cls[0])
                if class_id == PERSON_CLASS_ID:
                    detections.append(Detection(bbox_xyxy=xyxy, confidence=confidence, class_id=class_id))
        return detections

