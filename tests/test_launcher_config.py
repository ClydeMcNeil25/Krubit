from pathlib import Path


def test_launcher_allows_phase_two_environment_names() -> None:
    text = Path("scripts/invoke-krubit.ps1").read_text(encoding="utf-8")

    for name in (
        "TWITCH_KRUBIT_CLIENT_ID",
        "TWITCH_KRUBIT_CLIENT_SECRET",
        "KRUBIT_LIVE_SIGNALS_ENABLED",
    ):
        assert f'"{name}"' in text


def test_launcher_allows_phase_two_completion_environment_names_without_requiring_them() -> None:
    text = Path("scripts/invoke-krubit.ps1").read_text(encoding="utf-8")

    optional_names = (
        "YOUTUBE_KRUBIT_API_KEY",
        "YOUTUBE_KRUBIT_PUSH_CALLBACK_SECRET",
        "X_KRUBIT_BEARER_TOKEN",
        "META_KRUBIT_APP_ID",
        "META_KRUBIT_APP_SECRET",
        "META_KRUBIT_CALLBACK_BASE_URL",
        "TIKTOK_KRUBIT_CLIENT_KEY",
        "TIKTOK_KRUBIT_CLIENT_SECRET",
        "TIKTOK_KRUBIT_CALLBACK_BASE_URL",
        "KRUBIT_CREATOR_SIGNALS_ENABLED",
        "KRUBIT_SOCIAL_DELIVERY_ENABLED",
        "KRUBIT_CREDENTIAL_ENCRYPTION_KEY",
        "KRUBIT_CALLBACK_PUBLIC_BASE_URL",
        "KRUBIT_CALLBACK_PORT",
    )
    for name in optional_names:
        assert f'"{name}"' in text

    required_section = text.split("$requiredNames = @(")[1].split(")")[0]
    for name in optional_names:
        assert name not in required_section
