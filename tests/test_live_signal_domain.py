from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from krubit.domain.live_signals import (
    LiveSignalAction,
    LiveSignalPlan,
    LiveSignalSession,
    LiveSignalStatus,
    StreamingObservation,
    TwitchLookup,
    TwitchStream,
    normalize_twitch_channel,
    provisional_session_key,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://twitch.tv/KrucialStudios", "krucialstudios"),
        ("https://www.twitch.tv/krucialstudios/", "krucialstudios"),
        ("https://youtube.com/live/example", None),
        ("https://evil.example/twitch.tv/krucialstudios", None),
        ("https://twitch.tv/directory", None),
    ],
)
def test_normalize_twitch_channel(url: str, expected: str | None) -> None:
    assert normalize_twitch_channel(url) == expected


def test_provisional_identity_is_stable_for_replayed_presence() -> None:
    observation = observation_for_test()

    assert provisional_session_key(observation) == provisional_session_key(observation)


def test_observation_requires_positive_ids_and_aware_timestamps() -> None:
    with pytest.raises(ValueError, match="guild_id must be positive"):
        StreamingObservation(
            0, 222, "krucialstudios", "https://twitch.tv/krucialstudios", None, now()
        )
    with pytest.raises(ValueError, match="timestamp must include a timezone"):
        StreamingObservation(
            111,
            222,
            "krucialstudios",
            "https://twitch.tv/krucialstudios",
            None,
            datetime(2026, 8, 4),
        )


def test_live_signal_values_are_immutable_and_statuses_are_constrained() -> None:
    observation = observation_for_test()

    with pytest.raises(FrozenInstanceError):
        observation.guild_id = 333  # type: ignore[misc]

    assert [status.value for status in LiveSignalStatus] == [
        "detected",
        "live",
        "ending",
        "ended",
        "failed",
    ]


def test_twitch_stream_bounds_external_text() -> None:
    with pytest.raises(ValueError, match="title exceeds 300 characters"):
        TwitchStream(
            stream_id="stream-1",
            user_login="krucialstudios",
            user_name="Krucial Studios",
            title="x" * 301,
            game_name="Just Chatting",
            started_at=now(),
            thumbnail_url="https://example.test/preview.jpg",
        )


def test_domain_enums_reject_raw_strings() -> None:
    with pytest.raises(ValueError, match="kind must be a TwitchLookupKind"):
        TwitchLookup("offline")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="status must be a LiveSignalStatus"):
        LiveSignalSession(
            guild_id=111,
            session_key="session-1",
            member_id=222,
            twitch_login="krucialstudios",
            twitch_url="https://twitch.tv/krucialstudios",
            status="live",  # type: ignore[arg-type]
            detected_at=now(),
        )


def test_live_signal_plan_requires_immutable_supported_actions() -> None:
    with pytest.raises(ValueError, match="actions must be a tuple"):
        LiveSignalPlan(111, "session-1", [LiveSignalAction.ANNOUNCE])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="actions must contain LiveSignalAction values"):
        LiveSignalPlan(111, "session-1", ("announce",))  # type: ignore[arg-type]


def test_live_signal_plan_carries_immutable_execution_targets() -> None:
    plan = LiveSignalPlan(
        111,
        "session-1",
        (LiveSignalAction.REMOVE_ROLE,),
        member_id=222,
        role_id=333,
        delivery_attempt=2,
    )

    assert plan.member_id == 222
    assert plan.role_id == 333
    assert plan.delivery_attempt == 2
    with pytest.raises(ValueError, match="delivery_attempt must be positive"):
        LiveSignalPlan(111, "session-1", (), delivery_attempt=0)


@pytest.mark.parametrize(
    "url",
    [
        "https://twitch.tv//krucialstudios",
        "https://twitch.tv/krucialstudios//",
        "https://twitch.tv/krucialstudios/extra",
    ],
)
def test_normalize_twitch_channel_rejects_extra_path_delimiters(url: str) -> None:
    assert normalize_twitch_channel(url) is None


def test_twitch_url_is_bounded_including_query_and_fragment() -> None:
    over_limit_url = "https://twitch.tv/krucialstudios?" + "x" * 2_016

    assert len(over_limit_url) == 2_049
    assert normalize_twitch_channel(over_limit_url) is None
    with pytest.raises(ValueError, match="twitch_url exceeds 2048 characters"):
        StreamingObservation(111, 222, "krucialstudios", over_limit_url, None, now())


def observation_for_test() -> StreamingObservation:
    return StreamingObservation(
        guild_id=111,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://www.twitch.tv/krucialstudios",
        activity_started_at=datetime(2026, 8, 4, 20, 12, tzinfo=UTC),
        observed_at=datetime(2026, 8, 4, 20, 14, tzinfo=UTC),
    )


def now() -> datetime:
    return datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
