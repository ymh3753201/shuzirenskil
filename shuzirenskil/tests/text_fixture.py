"""Synthetic OCR evidence for solid-color fixtures ONLY; never production evidence."""
from pathlib import Path
from _common import atomic_write_json, read_json, sha256_file
from media import extract_review_frames
from text_inspection import inspect_text


def synthetic_text_inspection(video, folder, boundaries=None):
    folder = Path(folder)
    extract_review_frames(video, folder, boundaries or [])
    manifest = read_json(folder / 'manifest.json')
    atomic_write_json(folder / 'text-qc.json', {
        'schema_version': 'text-qc-1.0', 'video_sha256': sha256_file(Path(video)),
        'sampling': 'every_decoded_frame', 'status': 'no_text_detected', 'detections': [],
        'decoded_frames': manifest['total_video_frames'], 'engine': 'synthetic-test-only'})
    return inspect_text(video, folder, boundaries)


def viewed_indices(inspection):
    return [f['frame_index'] for f in read_json(Path(inspection['manifest_file']))['frames']]
