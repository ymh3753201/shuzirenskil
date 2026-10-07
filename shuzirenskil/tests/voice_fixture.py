from video_prompt import default_prompt_spec


def designed_voice_spec():
    spec = default_prompt_spec()
    spec["voice_profile"]["pitch"] = "中低音区，重点词轻抬音高"
    spec["voice_profile"]["timbre"] = "清亮偏薄，少量气息但不漏气"
    spec["voice_casting"] = {
        "character_key": "test-presenter", "role": "面向初学者的工具讲解者", "visual_basis": "测试方案中的白衬衫、短发造型",
        "rationale": "为轻松交流选择清亮声线，属于艺术选角并非推断真实声音",
        "resonance": "前口腔共鸣为主，胸腔厚度少", "texture": "干净清亮，保持轻微呼吸质感",
        "articulation": "辅音轻而清楚，句尾收得干净", "avoid": "避免播音腔、气泡音和刻意卖萌",
    }
    return spec
