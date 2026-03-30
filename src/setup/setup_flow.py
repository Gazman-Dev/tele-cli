from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .installer import current_installer
from core.json_store import load_json, save_json
from core.models import AuthState, Config
from core.paths import AppPaths
from core.prompts import ask_text
from integrations.telegram import TelegramClient, is_auth_paired
from storage.diagnostics import log_recovery_event
from .host_service import build_service_registration, current_service_manager, resolve_duplicate_registrations
from .pairing import complete_pending_pairing, pair_authorized_operator
from .recovery import SetupRecoveryChoices, initialize_setup
from .service_manager import ensure_service_registration
from .state import load_setup_state, save_setup_state


def _npm_global_bin() -> str | None:
    if not shutil.which("npm"):
        return None
    try:
        result = subprocess.run(
            ["npm", "config", "get", "prefix"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    prefix = (result.stdout or "").strip()
    if not prefix:
        return None
    candidate = Path(prefix) / "bin" / "codex"
    if candidate.exists():
        return str(candidate)
    return None


def resolve_codex_command(config: Config) -> list[str]:
    configured = list(config.codex_command or ["codex"])
    candidate = configured[0]
    if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
        return configured

    resolved = shutil.which(candidate)
    if resolved:
        return [resolved, *configured[1:]]

    npm_codex = _npm_global_bin()
    if npm_codex:
        return [npm_codex, *configured[1:]]

    return configured


def ensure_local_dependencies(paths: AppPaths, setup_state=None) -> list[str]:
    paths.root.mkdir(parents=True, exist_ok=True)
    installer = current_installer()
    config = load_json(paths.config, Config.from_dict) or Config(state_dir=str(paths.root))
    if not paths.config.exists():
        save_json(paths.config, config.to_dict())

    state = setup_state or load_setup_state(paths)
    steps: list[str] = []

    if shutil.which("npm"):
        if state is not None:
            state.npm_installed = True
    else:
        plan = installer.install_npm(allow_homebrew_install=config.install_homebrew_if_missing)
        installer.run(plan)
        steps.append(f"Installing npm via {plan.manager or 'system package manager'}")
        if state is not None:
            state.npm_installed = True
            save_setup_state(paths, state)

    if shutil.which("codex"):
        if state is not None:
            state.codex_installed = True
    else:
        plan = installer.install_codex()
        installer.run(plan)
        steps.append("Installing Codex CLI")
        if state is not None:
            state.codex_installed = True
            save_setup_state(paths, state)

    config.codex_command = resolve_codex_command(config)
    save_json(paths.config, config.to_dict())

    return steps


def run_setup(paths: AppPaths, recovery_choices: SetupRecoveryChoices | None = None) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    app_lock, setup_state = initialize_setup(paths, choices=recovery_choices)
    setup_state.pid = os.getpid()
    save_setup_state(paths, setup_state)
    config = load_json(paths.config, Config.from_dict) or Config(state_dir=str(paths.root))
    existing_auth = load_json(paths.auth, AuthState.from_dict)
    try:
        for step in ensure_local_dependencies(paths, setup_state):
            print(step)

        if existing_auth and existing_auth.bot_token:
            auth = existing_auth
            print("Reusing saved Telegram bot token from a previous setup attempt.")
        else:
            token = ask_text("Telegram bot token", secret=True)
            auth = AuthState(bot_token=token)
            save_json(paths.auth, auth.to_dict())
            setup_state.telegram_token_saved = True
            save_setup_state(paths, setup_state)

        bot = TelegramClient(auth.bot_token)
        bot.validate()
        setup_state.telegram_validated = True
        save_setup_state(paths, setup_state)

        if is_auth_paired(auth):
            print("Telegram chat is already paired. Keeping existing authorization.")
        else:
            pair_authorized_operator(paths, auth, bot)
        service_manager = current_service_manager()
        desired = build_service_registration(paths)
        result = ensure_service_registration(service_manager, desired)
        if not resolve_duplicate_registrations(service_manager, result, desired):
            raise RuntimeError("Duplicate service registrations were not repaired.")
        if result.action == "repair_required":
            result = ensure_service_registration(service_manager, desired)
        setup_state.status = "completed"
        save_setup_state(paths, setup_state)
        save_json(paths.config, config.to_dict())
        log_recovery_event(paths, "setup completed")
        app_lock.clear()
        print("Setup complete.")
    except Exception:
        setup_state.status = "failed"
        save_setup_state(paths, setup_state)
        log_recovery_event(paths, "setup failed")
        app_lock.clear()
        raise
