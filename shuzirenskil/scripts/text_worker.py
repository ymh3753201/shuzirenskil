"""Offline OCR of every decoded frame. Never interprets silence as no text."""
import argparse
import json
import tempfile
from pathlib import Path
from _common import atomic_write_json, sha256_file


def self_test():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
    blank = Image.new('RGB', (640, 160), 'white')
    empty, _ = engine(np.array(blank))
    ImageDraw.Draw(blank).text((30, 45), 'OCR TEST 123', font=ImageFont.load_default(size=40), fill='black')
    result, _ = engine(np.array(blank))
    text = ''.join(item[1] for item in result or []).replace(' ', '').upper()
    import av
    with tempfile.TemporaryDirectory(prefix='ocr-self-test-') as tmp:
        video, report_path = Path(tmp)/'single-last-frame.mp4', Path(tmp)/'text-qc.json'
        with av.open(str(video), 'w') as out:
            stream = out.add_stream('mpeg4', rate=2)
            stream.width, stream.height, stream.pix_fmt = 640, 160, 'yuv420p'
            for picture in [Image.new('RGB',(640,160),'white')]*2+[blank]:
                for packet in stream.encode(av.VideoFrame.from_ndarray(np.array(picture),format='rgb24')):out.mux(packet)
            for packet in stream.encode():out.mux(packet)
        scanned = scan(video, report_path, engine)
    last_frame = scanned['decoded_frames'] == 3 and bool(scanned['detections']) and all(d['frame_index'] == 2 for d in scanned['detections'])
    return {'passed': not empty and text == 'OCRTEST123' and last_frame, 'recognized': text,
            'single_last_frame_detected': last_frame,
            'fixture': 'local blank, visible Latin text, and 3-frame video with text only on last frame; not an accuracy guarantee'}


def scan(video, output, engine=None):
    import av
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
    source = sha256_file(video)
    report = {'schema_version': 'text-qc-1.0', 'video_sha256': source,
              'engine': 'rapidocr-onnxruntime-1.4.4', 'sampling': 'every_decoded_frame',
              'status': 'incomplete', 'decoded_frames': 0, 'detections': [],
              'network_calls': 0, 'perceptual_review': 'pending'}
    try:
        with av.open(str(video)) as container:
            for index, frame in enumerate(container.decode(video=0)):
                results, _ = engine(frame.to_ndarray(format='bgr24'))
                for box, text, confidence in results or []:
                    # Even weak/non-Chinese detections need inspection; no silent whitelist.
                    if str(text).strip():
                        report['detections'].append({'frame_index': index,
                            'time_seconds': float(frame.time) if frame.time is not None else None,
                            'text': str(text), 'confidence': float(confidence), 'box': box})
                report['decoded_frames'] += 1
        if not report['decoded_frames'] or source != sha256_file(video):
            raise ValueError('视频无画面或在检查时发生变化')
        report['status'] = 'text_detected' if report['detections'] else 'no_text_detected'
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        atomic_write_json(Path(output), report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        result = self_test()
        print(json.dumps(result))
        raise SystemExit(0 if result['passed'] else 1)
    if not args.video or not args.output:parser.error('需要 --video 和 --output')
    scan(args.video, args.output)
