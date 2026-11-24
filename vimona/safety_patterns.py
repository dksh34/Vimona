"""Shared sensitive/explicit text classification rules (zero heavy UI dependencies)."""

from __future__ import annotations

import re

EXPLICIT_TEXT_PATTERN = re.compile(
    r"\b("
    r"nsfw|nsfl|porn|pornography|pornographic|hentai|erotic|erotica|erotically|"
    r"xxx|nude|nudes|nudity|naked|topless|bottomless|uncensored sex|explicit sexual|explicit nsfw|"
    r"sex|sexual|sexually|intercourse|orgasm|orgasms|climax|masturbat\w*|"
    r"blowjob|handjob|footjob|rimjob|titjob|boobjob|cunnilingus|fellatio|deepthroat|"
    r"penis|cock|dick|phallus|vagina|pussy|clit|clitoris|vulva|labia|anus|anal|"
    r"boobs|tits|titties|breasts|nipple|nipples|areola|buttocks|buttcheeks|cumshot|ejaculat\w*|"
    r"semen|sperm|cum|creampie|bukkake|gangbang|threesome|foursome|orgy|bdsm|bondage|"
    r"fetish|kink|kinky|dominatrix|submissive slave|dildo|vibrator|buttplug|"
    r"lingerie|boudoir|bikini|skimpy|undress\w*|sensual|cleavage|"
    r"stripper|striptease|lap dance|escort|prostitut\w*|milf|gilf|incest|taboo sex|"
    r"horny|aroused|spread legs|cameltoe|upskirt|underboob|sideboob|"
    r"gore|gory|behead\w*|decapitat\w*|dismember\w*|eviscerat\w*|entrails|mutilat\w*|bloodbath|"
    r"bloodshed|bloody|carnage|corpse\w*|gruesome|visceral|"
    r"battle\w*|warfare|warzone|gunfight|shootout|casualt\w*|"
    r"massacre|slaughter|tortur\w*|murder\w*|self[- ]?harm"
    r")\b",
    re.IGNORECASE,
)


def is_explicit_text(text: str | None) -> bool:
    """Return True if prompt, query, or title contains explicit adult or graphic sensitive terms."""
    if not text:
        return False
    return bool(EXPLICIT_TEXT_PATTERN.search(text))
