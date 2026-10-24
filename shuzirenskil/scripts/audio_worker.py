"""Offline ASR + Silero VAD. Run only in the managed speech environment."""
import argparse
import json
import os
from pathlib import Path


def recognize(audio, model_path, language=None):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    samples = decode_audio(str(audio), sampling_rate=16000)
    regions = get_speech_timestamps(samples, VadOptions(min_silence_duration_ms=250, speech_pad_ms=0))
    model = WhisperModel(str(model_path), device='cpu', compute_type='int8', local_files_only=True)
    # Do not prompt with the approved script: recognition must describe actual audio.
    segments, info = model.transcribe(samples, language=language, word_timestamps=True,
        vad_filter=True, vad_parameters={'min_silence_duration_ms': 250},
        condition_on_previous_text=False, temperature=0, beam_size=5)
    cues, words = [], []
    for segment in segments:
        if not segment.text.strip():
            continue
        cues.append({'start_seconds': segment.start, 'end_seconds': segment.end,
                     'text': segment.text.strip(), 'avg_logprob': segment.avg_logprob,
                     'no_speech_prob': segment.no_speech_prob})
        words.extend({'start_seconds': word.start, 'end_seconds': word.end,
                      'text': word.word, 'probability': word.probability} for word in segment.words or [])
    return {'engine': 'faster-whisper', 'language': info.language,
            'model_path': str(Path(model_path).resolve()), 'network_calls': 0,
            'transcript': ''.join(cue['text'] for cue in cues), 'cues': cues, 'words': words,
            'speech_regions': [{'start_seconds': item['start']/16000, 'end_seconds': item['end']/16000} for item in regions]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--language')
    args = parser.parse_args()
    result = recognize(args.audio, args.model, args.language)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
