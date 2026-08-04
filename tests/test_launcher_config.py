from pathlib import Path


def test_launcher_allows_phase_two_environment_names() -> None:
    text = Path("scripts/invoke-krubit.ps1").read_text(encoding="utf-8")

    for name in (
        "TWITCH_KRUBIT_CLIENT_ID",
        "TWITCH_KRUBIT_CLIENT_SECRET",
        "KRUBIT_LIVE_SIGNALS_ENABLED",
    ):
        assert f'"{name}"' in text
