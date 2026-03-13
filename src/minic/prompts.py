from __future__ import annotations

from typing import Optional


def ask_choice(message: str, choices: list[str], default: Optional[str] = None) -> str:
    choice_set = {item.lower() for item in choices}
    prompt = "/".join(choices)
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{message} ({prompt}){suffix}: ").strip().lower()
        if not raw and default:
            return default
        if raw in choice_set:
            return raw
        print(f"Choose one of: {', '.join(choices)}")


def ask_text(message: str, secret: bool = False) -> str:
    if secret:
        import getpass

        return getpass.getpass(f"{message}: ").strip()
    return input(f"{message}: ").strip()
