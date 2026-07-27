import unittest

try:
    import cv2
    import numpy as np
    from app.clip_analyzer import ClipAnalyzer
    from app.quality import FaceQuality, measure_face_quality
except ImportError:
    cv2 = np = ClipAnalyzer = FaceQuality = measure_face_quality = None


@unittest.skipIf(cv2 is None, "OpenCV dependencies are not installed")
class QualityAndTrackingTests(unittest.TestCase):
    def test_clear_front_face_is_usable(self):
        class Face:
            bbox = np.array([20, 20, 100, 100])
            det_score = .95
            kps = np.array([
                [40, 48], [80, 48], [60, 62], [45, 80], [75, 80],
            ])

        image = np.zeros((120, 120, 3), dtype=np.uint8)
        for y in range(20, 100, 4):
            image[y:y + 2, 20:100] = 220
        result = measure_face_quality(
            image, Face(), min_face_px=48, min_quality=.2
        )
        self.assertTrue(result.usable)
        self.assertGreater(result.sharpness, 0)

    def test_track_assignment_separates_dissimilar_faces(self):
        analyzer = ClipAnalyzer(object(), object(), track_similarity=.55)
        tracks = []
        first = np.array([1.0, 0.0], dtype=np.float32)
        similar = np.array([.99, .01], dtype=np.float32)
        similar /= np.linalg.norm(similar)
        other = np.array([0.0, 1.0], dtype=np.float32)
        idx = analyzer._track_for(tracks, first)
        tracks[idx]["embeddings"].append(first)
        self.assertEqual(analyzer._track_for(tracks, similar), 0)
        self.assertEqual(analyzer._track_for(tracks, other), 1)
