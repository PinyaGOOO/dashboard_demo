"""Audience of a classroom publication. Only SQL NULL means unrestricted."""
import json


MAX_ASSIGNMENT_GROUPS = 256


def validate_student_groups(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_ASSIGNMENT_GROUPS:
        raise ValueError(f"Выберите не более {MAX_ASSIGNMENT_GROUPS} групп.")
    if any(not isinstance(g, str) or not 1 <= len(g) <= 80 or not g.isprintable() or g != g.strip()
           for g in value):
        raise ValueError("Название группы должно быть непустой строкой до 80 символов без краевых пробелов.")
    if len(set(value)) != len(value):
        raise ValueError("Группа не должна повторяться в списке.")
    return sorted(value, key=lambda g: (g.casefold(), g))


def parse_stored_groups(raw_policy):
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, str) or len(raw_policy) > 262144:
        return []
    try:
        value = json.loads(raw_policy)
        return validate_student_groups(value) if isinstance(value, list) else []
    except (ValueError, RecursionError):
        return []


def allows_student_group(policy, group):
    """Use a validated/decoded policy; never trust a student's request for group."""
    if policy is None:
        return True
    if not isinstance(group, str):
        return False
    try:
        return group in validate_student_groups(policy)
    except ValueError:
        return False
