"""Every-frame OCR plus actual visual review, shared by final and segment gates."""
import subprocess
from pathlib import Path
from _common import read_json, sha256_file
from media import extract_review_frames
from runtime_tools import settings


def inspect_text(video, folder, boundaries=None):
    video, folder = Path(video), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'text-qc.json'
    report = read_json(target) if target.is_file() else {}
    if (report.get('video_sha256') != sha256_file(video)
            or report.get('sampling') != 'every_decoded_frame'
            or report.get('schema_version') != 'text-qc-1.0'
            or report.get('status') not in {'no_text_detected', 'text_detected'}):
        python = settings().get('python')
        if not python or not Path(python).is_file():
            raise ValueError('缺少本地逐帧 OCR 环境，不能标记无字通过；先运行环境安装与自检')
        subprocess.run([python, str(Path(__file__).with_name('text_worker.py')),
                        '--video', str(video), '--output', str(target)],
                       check=True, capture_output=True, timeout=3600)
        report = read_json(target)
    extra = sorted({d['frame_index'] for d in report.get('detections', [])})
    frames = extract_review_frames(video, folder, boundaries or [], extra_indices=extra)
    manifest_path = folder / 'manifest.json'
    return {'manifest_file': str(manifest_path.resolve()), 'manifest_sha256': sha256_file(manifest_path),
            'text_qc_file': str(target.resolve()), 'text_qc_sha256': sha256_file(target),
            'reviewed_frame_indices': [], 'ocr_findings_review': [],
            'frame_count': len(frames), 'status': 'awaiting_visual_review', 'last_frame': frames[-1]}


def validate_ocr(review, video, manifest):
    path = Path(review.get('text_qc_file', ''))
    if not path.is_file() or sha256_file(path) != review.get('text_qc_sha256'):
        raise ValueError('缺少绑定当前视频的逐帧文字报告，或报告已改变')
    report = read_json(path)
    count = manifest.get('total_video_frames')
    if (report.get('schema_version') != 'text-qc-1.0' or report.get('video_sha256') != sha256_file(video)
            or report.get('sampling') != 'every_decoded_frame' or type(count) is not int or count <= 0
            or type(report.get('decoded_frames')) is not int or report['decoded_frames'] != count
            or report.get('status') not in {'no_text_detected', 'text_detected'}
            or not isinstance(report.get('detections'), list)):
        raise ValueError('逐帧文字检查不完整、少帧或与当前视频不符')
    findings = report['detections']
    if bool(findings) != (report['status'] == 'text_detected'):
        raise ValueError('文字检测状态与内容不一致')
    indices = [f['frame_index'] for f in manifest['frames']]
    viewed = review.get('reviewed_frame_indices')
    if (not isinstance(viewed, list) or any(type(i) is not int for i in viewed)
            or len(viewed) != len(indices) or set(viewed) != set(indices)
            or 0 not in indices or count - 1 not in indices):
        raise ValueError('必须逐项记录已看取样，包含首末帧与全部疑似文字帧')
    for finding in findings:
        i = finding.get('frame_index')
        if type(i) is not int or not 0 <= i < count or i not in indices:
            raise ValueError('疑似文字的原帧未纳入检查')
    reviews = review.get('ocr_findings_review', [])
    if (not isinstance(reviews, list) or len(reviews) != len(findings)
            or any(not isinstance(r, dict) or type(r.get('detection_index')) is not int for r in reviews)
            or sorted(r['detection_index'] for r in reviews) != list(range(len(findings)))
            or any(r.get('verdict') != 'false_positive' or not isinstance(r.get('observations'), str)
                   or len(r['observations'].strip()) < 8 for r in reviews)):
        raise ValueError('疑似文字必须逐项看原帧；仅真实误检可排除，真实文字必须失败')
    return report
