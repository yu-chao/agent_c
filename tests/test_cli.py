from agent.cli import create_cli_identity


def test_cli_identity_uses_named_session_and_unique_message_ids():
    first = create_cli_identity("project-a")
    second = create_cli_identity("project-a")

    assert first.platform == "cli"
    assert first.conversation_id == "project-a"
    assert first.sender_id == "local-user"
    assert first.message_id != second.message_id
