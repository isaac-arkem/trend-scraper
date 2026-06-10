def dedupe_by_username(profiles: list[dict], platform_key: str = "username") -> list[dict]:
    seen = set()
    result = []
    for p in profiles:
        key = (p.get("platform", ""), p.get(platform_key, "").lower())
        if key not in seen and key[1]:
            seen.add(key)
            result.append(p)
    return result


def dedupe_list(items: list, key_fn) -> list:
    seen = set()
    result = []
    for item in items:
        k = key_fn(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def merge_profiles(existing: list[dict], new_profiles: list[dict]) -> list[dict]:
    existing_usernames = {p.get("username", "").lower() for p in existing}
    additions = [p for p in new_profiles if p.get("username", "").lower() not in existing_usernames]
    return existing + additions
