__all__ = ["run_uninstall", "run_update", "complete_pending_pairing", "run_setup"]


def __getattr__(name: str):
    if name in {"run_uninstall", "run_update"}:
        from .admin import run_uninstall, run_update

        return {"run_uninstall": run_uninstall, "run_update": run_update}[name]
    if name in {"complete_pending_pairing", "run_setup"}:
        from .setup_flow import complete_pending_pairing, run_setup

        return {"complete_pending_pairing": complete_pending_pairing, "run_setup": run_setup}[name]
    raise AttributeError(name)

