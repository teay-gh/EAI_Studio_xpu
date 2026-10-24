"""蒸馏提示词的文案表（用户 2026-10-20）。

口径（用户原话）：
  · 中文模式下蒸馏数据默认中文，英文模式下蒸馏数据默认英文；
  · 「这个中英文提示不要隐藏发送，直接写在提示词里」——
    所以语言要求写在 system 提示词正文里（见 core.config 的 DEFAULT_CONFIG），
    这里只负责**用户消息**（出题 / 写作 / 审阅 / 字数提示）跟着界面语言走。

为什么用户消息也要翻译：英文界面下 system 提示词是英文，若用户消息仍是中文，
模型多半还是吐中文，蒸馏数据就跟着语言脱钩了。
"""

from __future__ import annotations


def lang_of(value: object) -> str:
    """把界面语言归一成 'zh' / 'en'。"""
    s = str(value or "").strip().lower()
    return "en" if s.startswith("en") else "zh"


# ---------------------------------------------------------------- 语料蒸馏
def title_user(lang: str, index: int, theme: str) -> str:
    if lang == "en":
        return ("Draft a title for article #%d." % (index + 1)
                + (" Theme: %s." % theme if theme else " Pick a theme yourself.")
                + " The title must name one concrete subject.")
    return ("请为第 %d 篇文章拟定一个标题。" % (index + 1)
            + ("主题方向：%s。" % theme if theme else "主题不限，请自选一个具体方向。")
            + "标题要能落在一个具体话题上。")


def length_hint(lang: str, lo: int, hi: int) -> str:
    """正文长度提示；0 表示不限（用户 2026-10-19 口径）。"""
    if lang == "en":
        if hi and lo:
            return "Keep the body between %d and %d words." % (lo, hi)
        if hi:
            return "Keep the body under %d words." % hi
        if lo:
            return "The body must be at least %d words." % lo
        return ""
    if hi and lo:
        return "正文控制在 %d-%d 字之间。" % (lo, hi)
    if hi:
        return "正文控制在 %d 字以内。" % hi
    if lo:
        return "正文不少于 %d 字。" % lo
    return ""


def write_user(lang: str, title: str, hint: str) -> str:
    if lang == "en":
        return ('Write a complete article titled "%s". %s'
                " Output the body only: no title line, no notes." % (title, hint))
    return ("请围绕标题《%s》撰写一篇完整文章。%s"
            "只输出正文：不要重复标题，不要任何说明。" % (title, hint))


def article_review_user(lang: str, title: str, article: str) -> str:
    if lang == "en":
        return ('Title: %s\n\nBody:\n%s\n\n'
                "Check this article for major errors and output JSON only." % (title, article))
    return ("标题：%s\n\n正文：\n%s\n\n"
            "请审阅这篇正文是否存在重大错误，只输出 JSON。" % (title, article))


# ---------------------------------------------------------------- 响应蒸馏
def answer_hint(lang: str, lo: int, hi: int, for_questioner: bool = False) -> str:
    """回答字数的追加提示（用户 2026-10-19 / 2026-10-20）。

    出题侧也要一份：让问题落在能答进这个篇幅的范围内。
    """
    if lang == "en":
        if hi and lo:
            base = "Keep the answer between %d and %d words" % (lo, hi)
        elif hi:
            base = "Keep the answer under %d words" % hi
        elif lo:
            base = "The answer must be at least %d words" % lo
        else:
            return ""
        if for_questioner:
            base += ", so pick a question that fits that length"
        return "(%s.)" % base
    if hi and lo:
        base = "回答控制在 %d-%d 字之间" % (lo, hi)
    elif hi:
        base = "回答控制在 %d 字以内" % hi
    elif lo:
        base = "回答不少于 %d 字" % lo
    else:
        return ""
    if for_questioner:
        base += "，问题要能在这个篇幅内答完"
    return "（%s。）" % base


def question_user(lang: str, index: int, topic: str, auto: bool,
                  hint: str, reask: str = "") -> str:
    if lang == "en":
        if auto:
            user = ("Write question #%d." % (index + 1)
                    + (" Topic: %s." % topic if topic else " Any worthwhile topic of your choice.")
                    + " Medium-to-hard difficulty, useful for knowledge distillation."
                    " Output the question only.")
        else:
            user = 'Write one question about "%s". Output the question only.' % topic
        user += hint
        if reask:
            user += (" " if lang == "en" else "") + reask
        return user
    if auto:
        user = ("请生成第 %d 个问题。" % (index + 1)
                + ("主题方向：%s。" % topic if topic else "主题不限，可自行选择有价值的方向。")
                + "难度适中偏高，适合用来做知识蒸馏数据。只输出问题。")
    else:
        user = "围绕主题「%s」生成一个问题，只输出问题本身。" % topic
    return user + hint + reask


def resp_review_user(lang: str, question: str, answer: str) -> str:
    if lang == "en":
        return ("Question: %s\n\nAnswer:\n%s\n\n"
                "Review this pair for major errors and output JSON only." % (question, answer))
    return ("问题：%s\n\n回答：\n%s\n\n"
            "请快速审阅该问答对是否存在重大问题，并只输出 JSON。" % (question, answer))


# 「重复率过线 → 换一种问法」的后缀（用户 2026-10-21：对用户不可见，只进提示词）
_REASK = {
    "zh": (
        "（换一种问法：不要与此前的问题重复。）",
        "（换一种问法：换一个切入角度，避免与此前的问题雷同。）",
        "（换一种问法：换一个子话题，避免与此前的问题雷同。）",
        "（换一种问法：明显换个方向，别和此前的问题撞题。）",
        "（换一种问法：彻底换个方向，此前的问题都别再碰。）",
    ),
    "en": (
        "(Ask it differently: do not repeat earlier questions.)",
        "(Ask it differently: take another angle, avoid overlap with earlier questions.)",
        "(Ask it differently: pick a different subtopic, avoid overlap with earlier questions.)",
        "(Ask it differently: change direction clearly, do not collide with earlier questions.)",
        "(Ask it differently: change direction completely, avoid all earlier questions.)",
    ),
}


def reask_hint(lang: str, level: int) -> str:
    if level <= 0:
        return ""
    table = _REASK["en" if lang == "en" else "zh"]
    return table[min(4, int(level))]
