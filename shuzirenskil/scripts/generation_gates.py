"""Fail closed on known quality failures and on undersized provider originals."""
from pathlib import Path
from _common import read_json, atomic_write_json, sha256_file
from media import technical_summary


def require_no_quality_failure(root):
    # Both names have been emitted by previous agents/workflows. Keep originals.
    for name in ('quality-failure.json', 'quality-failure-report.json'):
        path = Path(root) / name
        if path.is_file():
            report = read_json(path)
            checks = report.get('checks', {})
            failed = report.get('status') == 'fail' or any(
                isinstance(x, dict) and x.get('status') == 'fail'
                for x in checks.values())
            if failed:
                raise ValueError(f'已记录明确质量缺陷（{name}）；停止后续生成、拼接与交付，不自动备用或重做')


def require_original_dimensions(video, settings):
    meta = technical_summary(Path(video))
    minimum = {'480p': 480, '720p': 720}.get(settings.get('resolution'))
    ratio = {'9:16': 9 / 16, '16:9': 16 / 9}.get(settings.get('aspect_ratio'))
    width, height = meta.get('width', 0), meta.get('height', 0)
    if (not minimum or not ratio or not width or not height
            or min(width, height) < minimum or abs(width / height - ratio) >= 0.01):
        raise ValueError(f'Provider 原片尺寸 {width}×{height} 未达到已确认的分辨率/画幅；不能放大后冒充原片合格')
    return meta


def record_confirmed_text_failure(root, video, evidence, binding):
    """Persist an explicit finding; OCR suspicion alone is never a confirmed fail."""
    review = evidence.get('text_review', evidence)
    if (review.get('reviewer') not in {'agent_visual', 'human_user'}
            or review.get('text_present') is not True
            or not isinstance(review.get('observations'), str)
            or len(review['observations'].strip()) < 8):
        return
    video = Path(video)
    report = {'status': 'fail', 'business_accepted': False, 'video_sha256': sha256_file(video),
              'contract_digest': binding, 'automatic_redo_authorized': False,
              'checks': {'no_generated_text': {'status': 'fail', 'observations': review['observations']}},
              'evidence': evidence}
    # Each finding is immutable; the standard pointer is what every entry checks.
    archive = Path(root) / 'quality-failures' / (report['video_sha256'] + '.json')
    if not archive.exists():
        atomic_write_json(archive, report)
    pointer = Path(root) / 'quality-failure.json'
    if not pointer.exists():
        atomic_write_json(pointer, report)
