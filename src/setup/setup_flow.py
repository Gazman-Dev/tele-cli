from __future__ import annotations

import os
import shutil

from .installer import current_installer
from core.json_store import load_json, save_json
from core.logging_utils import append_recovery_log
from core.models import AuthState, Config
from core.paths import AppPaths
from core.prompts import ask_text
from integrations.telegram import TelegramClient, is_auth_paired
from .host_service import build_service_registration, current_service_manager, resolve_duplicate_registrations
from .pairing import complete_pending_pairing, pair_authorized_operator
from .recovery import handle_existing_setup as _handle_existing_setup
from .recovery import initialize_setup
from .service_manager import ensure_service_registration
from .state import save_setup_state


def run_setup(paths: AppPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    app_lock, setup_state = initialize_setup(paths)
    setup_state.pid = os.getpid()
    save_setup_state(paths, setup_state)
    installer = current_installer()
    config = load_json(paths.config, Config.from_dict) or Config(state_dir=str(paths.root))
    existing_auth = load_json(paths.auth, AuthState.from_dict)
    try:
        if shutil.which("npm"):
            setup_state.npm_installed = True
        else:
            plan = installer.install_npm(allow_homebrew_install=config.install_homebrew_if_missing)
            print(f"Installing npm via {plan.manager}: {' '.join(plan.command)}")
            installer.run(plan)
            setup_state.npm_installed = True
            save_setup_state(paths, setup_state)

        if shutil.which("codex"):
            setup_state.codex_installed = True
        else:
            plan = installer.install_codex()
            print(f"Installing Codex: {' '.join(plan.command)}")
            installer.run(plan)
            setup_state.codex_installed = True
            save_setup_state(paths, setup_state)

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
        append_recovery_log(paths.recovery_log, "setup completed")
        app_lock.clear()
        print("Setup complete.")
    except Exception:
        setup_state.status = "failed"
        save_setup_state(paths, setup_state)
        append_recovery_log(paths.recovery_log, "setup failed")
        app_lock.clear()
        raise
