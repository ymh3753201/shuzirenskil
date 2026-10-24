"""Production clearance is NOT full perceptual or business acceptance.

Only plans that disclosed continuous_candidate before authorization may use this.
Historical plans and locked audio/advertising contracts retain full review gates.
"""
import math
import subprocess
from pathlib import Path
from _common import atomic_write_json, read_json, sha256_file, digest_json
from audio_qc import inspect_audio, evidence_valid, normalized, tail_decision
from media import extract_review_frames
from runtime_tools import settings

MODE = 'continuous_candidate'
NOTICE = '按已确认范围连续生成、检查、裁尾和拼接候选片；不逐段请用户确认。已发现缺陷仍停止；缺少主观看听能力的项目留到全片统一验收，不宣称合格交付。'


def require_runtime():
    config = settings()
    python = config.get('python')
    if not python or not Path(python).is_file() or not Path(config.get('model', ''), 'model.bin').is_file():
        raise ValueError('连续制作需要本地语音识别与文字检查环境；先运行安装和 doctor --self-test，再确认制作')
    subprocess.run([python, '-c', 'from rapidocr_onnxruntime import RapidOCR; from faster_whisper.vad import get_vad_model; import opencc; get_vad_model(); RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)'], check=True, capture_output=True, timeout=90)


def inspect(video, script, folder, binding):
    video, folder = Path(video), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder/'manifest.json'
    if not manifest.is_file() or read_json(manifest).get('video_sha256') != sha256_file(video):
        extract_review_frames(video, folder)
    audio_file = folder/'audio-qc.json'
    audio = read_json(audio_file) if audio_file.exists() else {}
    if not evidence_valid(audio, video, script):
        audio = inspect_audio(video, folder, script)
    ocr_file = folder/'text-qc.json'
    ocr = read_json(ocr_file) if ocr_file.exists() else {}
    if ocr.get('video_sha256') != sha256_file(video) or ocr.get('status') not in {'no_text_detected', 'text_detected'}:
        require_runtime()
        subprocess.run([settings()['python'], str(Path(__file__).with_name('text_worker.py')),
                        '--video', str(video), '--output', str(ocr_file)], check=True, timeout=3600)
        ocr = read_json(ocr_file)
    finding_frames = sorted({d['frame_index'] for d in ocr.get('detections', [])})
    if not set(finding_frames).issubset({f['frame_index'] for f in read_json(manifest)['frames']}):
        extract_review_frames(video, folder, extra_indices=finding_frames)
    template = {'review_scope': 'production_clearance', 'contract_digest': binding,
        'video_sha256': sha256_file(video), 'business_accepted': False,
        'audio_qc_file': str(audio_file), 'audio_qc_sha256': sha256_file(audio_file),
        'text_qc_file': str(ocr_file), 'text_qc_sha256': sha256_file(ocr_file),
        'manifest_file': str(manifest), 'manifest_sha256': sha256_file(manifest),
        'reviewer': None, 'reviewed_frame_indices': [], 'text_present': None,
        'visible_defect_present': None, 'observations': '', 'perceptual_review': 'pending', 'ocr_findings_review': []}
    target = folder/'production-review-template.json'
    atomic_write_json(target, template)
    return {'status': 'awaiting_agent_ocr_review' if ocr.get('detections') else 'awaiting_agent_frame_review',
            'production_review_template': str(target), 'manifest_file': str(manifest),
            'audio_qc_file': str(audio_file), 'text_qc_file': str(ocr_file),
            'next_action': 'AI 查看清单内每一张取样后填写真实证据并运行 review-production；不是询问用户是否继续。发现文字禁止建议保留、打码或放宽要求。'}


def validate(evidence, video, script, binding):
    video = Path(video)
    if (evidence.get('review_scope') != 'production_clearance' or evidence.get('contract_digest') != binding
            or evidence.get('video_sha256') != sha256_file(video)
            or evidence.get('business_accepted') is not False or evidence.get('perceptual_review') != 'pending'):
        raise ValueError('继续制作证据必须绑定当前合同和视频，且不能冒充完整验收')
    reports = {}
    for kind in ('audio_qc', 'text_qc', 'manifest'):
        path = Path(evidence.get(kind+'_file', ''))
        if not path.is_file() or sha256_file(path) != evidence.get(kind+'_sha256'):
            raise ValueError('继续制作检查文件缺失或已改变：'+kind)
        reports[kind] = read_json(path)
    audio, ocr, manifest = (reports[k] for k in ('audio_qc', 'text_qc', 'manifest'))
    if not evidence_valid(audio, video, script) or audio.get('status') != 'automatic_checks_completed':
        raise ValueError('音频检查未完成')
    recognition = audio['recognition']
    if not normalized(script) or normalized(recognition.get('transcript', '')) != normalized(script):
        raise ValueError('实际识别台词与确认稿不符；不得用确认稿覆盖识别结果')
    technical = audio.get('technical', {})
    if not technical.get('has_audio') or not technical.get('has_video'):
        raise ValueError('视频缺画面或音轨')
    if (ocr.get('schema_version') != 'text-qc-1.0' or ocr.get('video_sha256') != sha256_file(video)
            or ocr.get('sampling') != 'every_decoded_frame' or ocr.get('status') not in {'no_text_detected', 'text_detected'}
            or not isinstance(ocr.get('detections'), list) or ocr.get('decoded_frames', 0) <= 0
            or ocr.get('decoded_frames') != manifest.get('total_video_frames')):
        raise ValueError('逐帧文字检查未完整通过或发现文字；禁止带字放行')
    if bool(ocr['detections']) != (ocr['status'] == 'text_detected'):
        raise ValueError('文字报告状态与检测结果不一致')
    findings = ocr['detections']
    if findings:
        reviews = evidence.get('ocr_findings_review', [])
        if (len(reviews) != len(findings) or sorted(r.get('detection_index', -1) for r in reviews) != list(range(len(findings)))
                or any(r.get('verdict') != 'false_positive' or len(r.get('observations', '').strip()) < 8 for r in reviews)):
            raise ValueError('OCR 疑似文字须逐项看对应原帧核实；确认文字必须 fail，不能许可保留')
    frames = manifest.get('frames', [])
    indices = [f['frame_index'] for f in frames]
    viewed = evidence.get('reviewed_frame_indices')
    if (manifest.get('video_sha256') != sha256_file(video)
            or manifest.get('sampling') != '2fps_plus_dense_tail_and_exact_first_last'
            or not indices or 0 not in indices or ocr['decoded_frames']-1 not in indices
            or not isinstance(viewed, list) or any(type(x) is not int for x in viewed)
            or len(viewed) != len(indices) or set(viewed) != set(indices)):
        raise ValueError('AI 必须查看全部取样、首帧和末帧；少量抽帧不能放行')
    if not {d['frame_index'] for d in findings}.issubset(set(indices)):
        raise ValueError('疑似文字对应的实际帧尚未加入视觉检查清单')
    for frame in frames:
        if sha256_file(Path(frame['path'])) != frame['sha256']:
            raise ValueError('已查看画面已变化')
    if (evidence.get('reviewer') not in {'agent_visual', 'human_user'} or evidence.get('text_present') is not False
            or evidence.get('visible_defect_present') is not False
            or not isinstance(evidence.get('observations'), str) or len(evidence['observations'].strip()) < 8):
        raise ValueError('画面有文字、明确缺陷或尚未完成真实视觉检查')
    # Known failures are never converted to a deferred subjective review.
    if any(isinstance(item, dict) and item.get('status') == 'fail' for item in evidence.get('checks', {}).values()):
        raise ValueError('已发现缺陷，不能以统一验收为由继续')
    cues = recognition.get('cues', [])
    words = recognition.get('words', [])
    duration = technical['duration_seconds']
    if not cues or not words or not recognition.get('speech_regions'):
        raise ValueError('语音时间码或语音活动证据缺失')
    previous = 0
    for cue in cues:
        start, end = cue.get('start_seconds'), cue.get('end_seconds')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or not previous <= start < end <= duration+0.05):
            raise ValueError('语音时间码无效或重叠')
        previous = end
    if normalized(''.join(c['text'] for c in cues)) != normalized(script):
        raise ValueError('时间码文本与实际识别不一致')
    if any(type(w.get('probability')) not in (int, float) or not math.isfinite(w['probability']) or not 0 <= w['probability'] <= 1 for w in words):
        raise ValueError('识别置信度证据无效')
    # Conservative auto tail editing retains raw recognition and evidence bindings.
    return {'cues': cues, 'transcript': recognition['transcript'],
            'tail_edit': tail_decision(video, script, cues, audio), 'business_accepted': False,
            'perceptual_review': 'pending', 'evidence_digest': digest_json(evidence),
            'low_confidence_words': [w for w in words if w['probability'] < 0.7]}
