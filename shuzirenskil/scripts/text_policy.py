"""Shared text-free generation policy. Local checks cannot guarantee model obedience."""
import re

POLICY_VERSION = 'text-free-2026-11-06'
NO_TEXT_RULE = (
    '采用纯摄影画面，仅人物、实景与自然动作。画面从第一帧到最后一帧均不出现任何文字、字幕、标题、'
    '金句、片尾卡、Logo、水印或 UI；台词和表演触发词仅用于声音与动作，不得转为屏幕文字。'
    '人物说到数字、列表、重点词或举手计数时，只用口述与手势表达，不出现对应字符或动态图文。'
    '服装、书脊、屏幕、包装和背景表面均无可读字符，包括外文、数字与伪文字。'
    '末句说完继续保持同一人物和场景，自然收声，不生成总结字卡。'
)
OPENING = ('画面硬约束：全程无字的原始摄影镜头，只有人物与实景。'
           'Text-free raw camera footage. No on-screen text, subtitles, captions, typography, '
           'letters, numbers, logos, watermarks or end cards. '
           'All dialogue is audible speech only; never render it visually.')
ENDING = '最终画面检查要求：' + NO_TEXT_RULE

# Deliberately bounded conflict lint, not a natural-language proof. Dialogue is
# excluded: a speaker may discuss subtitles without requesting visible subtitles.
_TEXT = r'(?:字幕|标题|文字|文本|字体|字卡|金句|标语|大字|小字|标识|招牌|水印|logo|UI|captions?|subtitles?|typography|text|letters|numbers|title\s*cards?)'
_POSITIVE = re.compile(
    rf'(?:添加|加上|显示|展示|出现|浮现|叠加|打出|写上|写着|写有|印有|保留|生成|弹出|配上|带有).{{0,12}}{_TEXT}'
    r'|(?:显示|写上|写着|写有|印有|打出|浮现|弹出)\s*[“「『\"]'
    rf'|{_TEXT}.{{0,12}}(?:浮现|弹出|上屏|显示|叠加|出现|动画|动效)'
    rf'|\b(?:add|show|display|render|overlay|include|keep|with)\b.{{0,30}}\b{_TEXT}\b', re.I)
_NEGATIVE = re.compile(
    r'(?:禁止|不得|不要|不能|不允许|无需|避免|去掉|移除|不)'
    r'(?:在(?:任何)?(?:画面|屏幕|背景|片尾|服装)(?:中|上)?)?'
    r'(?:再|额外|任何|所有|自动|擅自|新增|出现|生成|显示|添加|保留)?\s*$'
    r'|(?:无|没有)\s*$|\b(?:no|not|never|without|avoid|remove|disable)\s*$', re.I)


def validate_direction(value, path='visual_direction'):
    """Reject common contradictory directions; never silently edit confirmed input."""
    if isinstance(value, dict):
        for key, item in value.items():
            validate_direction(item, f'{path}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_direction(item, f'{path}[{index}]')
    elif isinstance(value, str):
        # Split contrast clauses too: "不要字幕，但显示标题" is a conflict.
        for clause in re.split(r'[。；;，,！!？?\n]|但是|但|然而|并且|然后|同时|并|\bbut\b|\bhowever\b|\band\b', value, flags=re.I):
            for match in _POSITIVE.finditer(clause):
                prefix = clause[:match.start()]
                if not _NEGATIVE.search(prefix):
                    raise ValueError(f'{path} 与无字要求冲突：请删除画面加字指令，并重新确认所改方案')


def require_text_free_ad(ad):
    if ad and ad.get('source_text'):
        raise ValueError('新生成必须全画面无字，不能沿用原生文字白名单；请先确认无字素材与广告方案，不自动抹掉真实招牌')


def audio_only_dialogue(script, language='所选语言'):
    # JSON quotes/newlines and template delimiters must not escape the audio block.
    if any(tag in script for tag in ('<SPOKEN_AUDIO_ONLY>', '</SPOKEN_AUDIO_ONLY>')):
        raise ValueError('口播稿包含保留的声音区块标记')
    return (f'台词声音轨：同一位主体用{language}完整说出以下原文；只发声，不显示原文，不朗读区块标记。\n'
            '<SPOKEN_AUDIO_ONLY>\n' + script + '\n</SPOKEN_AUDIO_ONLY>')


def guard_prompt(body):
    return OPENING + '\n' + body + '\n' + ENDING


def require_guarded_prompt(prompt):
    if not isinstance(prompt, str) or not prompt.startswith(OPENING) or not prompt.endswith(ENDING):
        raise ValueError('待提交请求缺少新版无字约束；先本地重新预检/准备方案，不能直接提交旧请求或自动重做')
