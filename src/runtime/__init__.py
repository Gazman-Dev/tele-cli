from .runtime import ServiceRuntime

__all__ = ["ServiceRuntime", "reset_auth", "run_service"]


def __getattr__(name: str):
    if name in {"reset_auth", "run_service"}:
        from .service import reset_auth, run_service

        return {"reset_auth": reset_auth, "run_service": run_service}[name]
    raise AttributeError(name)

