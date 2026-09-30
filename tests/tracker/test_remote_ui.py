from pathlib import Path


def test_remote_launcher_uses_pinned_host_and_owned_loopback_tunnel():
    from app.tracker.remote_ui import ssh_command
    # Deliberately not shaped like a home directory: the key path is never
    # asserted on, and such a literal trips the privacy scan's user_path rule.
    command = ssh_command('ssh', Path('/synthetic/keys/monolith_server_ed25519'))
    assert command[-1] == 'monolithic@192.168.1.50'
    assert 'StrictHostKeyChecking=yes' in command
    assert 'BatchMode=yes' in command
    assert '127.0.0.1:8792:127.0.0.1:8791' in command
    assert 'ExitOnForwardFailure=yes' in command
