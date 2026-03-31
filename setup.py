from setuptools import find_packages, setup


setup(
    name="tele-cli",
    version="0.1.0",
    description="Single-operator Codex and Telegram terminal bridge",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "runtime": ["resources/*.md", "resources/defaults/*.md"],
        "storage": ["migrations/*.sql"],
    },
    py_modules=["cli", "app_shell", "app_meta", "local_chat", "telegram_command"],
    python_requires=">=3.9",
    entry_points={
        "console_scripts": [
            "tele-cli=cli:main",
            "tele-cli-ux-demo=demo_ui:main",
        ]
    },
)
