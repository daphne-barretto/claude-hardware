"""The scaffold's core dependencies are installed and usable."""


def test_build123d_makes_an_exact_solid() -> None:
    from build123d import Box

    box = Box(10, 20, 30)
    assert box.is_valid
    assert abs(box.volume - 6000) < 1e-6


def test_claude_agent_sdk_imports() -> None:
    import claude_agent_sdk

    assert hasattr(claude_agent_sdk, "query")
