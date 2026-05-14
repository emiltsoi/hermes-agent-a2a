"""Agent Card helpers."""


def skill_names(agent_info: dict) -> set[str]:
    if not isinstance(agent_info, dict):
        return set()
    known_skills = agent_info.get("metadata", {}).get("skills", []) or agent_info.get("skills", [])
    return {
        str(item.get("name") or item.get("id") or "").lower()
        for item in known_skills
        if isinstance(item, dict) and (item.get("name") or item.get("id"))
    }


def validate_skill(agent_info: dict, skill: str) -> tuple[bool, list[str]]:
    names = skill_names(agent_info)
    if not skill or not names:
        return True, sorted(names)
    return skill.lower() in names, sorted(names)
