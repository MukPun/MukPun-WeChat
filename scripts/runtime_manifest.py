"""Explicit public-code and worker-role delivery inventory.

Never discover runtime/staging/private trees. Register new modules here together
with their role dependencies. Native retains its current flat /opt layout;
role maps can explicitly map nested package destinations in later tasks.
"""
from pathlib import Path, PurePosixPath
import stat

PUBLIC_CODE = (
    'scripts/accept_forwarder_core.py',
    'scripts/accept_reboot_recovery.py',
    'scripts/bridge_delivery_worker.py',
    'scripts/bridge_host_worker.py',
    'scripts/bridge_native_protocol.py',
    'scripts/bridge_native_worker.py',
    'scripts/check_astrbot_onebot.py',
    'scripts/confirm_private_image_delivery.py',
    'scripts/contact_receive_protocol.py',
    'scripts/contact_send_protocol.py',
    'scripts/deploy_all_groups.py',
    'scripts/deploy_group_context.py',
    'scripts/deploy_image_capacity_guard.py',
    'scripts/deploy_native_recovery.py',
    'scripts/deploy_sender_names.py',
    'scripts/deploy_storage_capacity.py',
    'scripts/diagnose_forwarder_startup.py',
    'scripts/empty_client_probe.py',
    'scripts/forward_host_worker.py',
    'scripts/forward_native_worker.py',
    'scripts/forward_sse.py',
    'scripts/forwarder_alert.py',
    'scripts/group_metadata_protocol.py',
    'scripts/group_reader_client.py',
    'scripts/group_receive_protocol.py',
    'scripts/group_send_protocol.py',
    'scripts/image_input_export.py',
    'scripts/image_native.py',
    'scripts/image_output_budget.py',
    'scripts/image_send_native.py',
    'scripts/image_send_recovery.py',
    'scripts/image_source_cache.py',
    'scripts/inspect_astrbot_context_settings.py',
    'scripts/inspect_astrbot_image_settings.py',
    'scripts/inspect_astrbot_readonly.py',
    'scripts/inspect_bridge_delivery.py',
    'scripts/inspect_failed_meme.py',
    'scripts/inspect_forwarder_outage_reason.py',
    'scripts/inspect_group_image_delivery.py',
    'scripts/inspect_image_capacity.py',
    'scripts/inspect_image_job.py',
    'scripts/inspect_meme_output_options.py',
    'scripts/inspect_saved_session.py',
    'scripts/inspect_sim_failure.py',
    'scripts/inspect_snapshot_outbox.py',
    'scripts/inspect_storage_runtime.py',
    'scripts/inspect_storage_switch.py',
    'scripts/login_client_probe.py',
    'scripts/maintain_media_cache.py',
    'scripts/media_cache_maintenance.py',
    'scripts/media_egress.py',
    'scripts/media_maintenance_policy.py',
    'scripts/media_maintenance_snapshot.py',
    'scripts/migrate_recovery_state.py',
    'scripts/native_health_probe.py',
    'scripts/nickname_probe_worker.py',
    'scripts/prepare-network-modules.py',
    'scripts/prepare_storage_release.py',
    'scripts/probe-ip-egress-guard.py',
    'scripts/probe-owned-netns.py',
    'scripts/probe-slirp-tap.py',
    'scripts/probe-tun-mounts.py',
    'scripts/probe_debug_sticker.py',
    'scripts/probe_group_nicknames.py',
    'scripts/probe_image_output.py',
    'scripts/probe_large_storage.py',
    'scripts/probe_loop_guard.py',
    'scripts/probe_meme_memory.py',
    'scripts/probe_onebot_simulation.py',
    'scripts/probe_storage_host_guard.py',
    'scripts/project_paths.py',
    'scripts/qr_login_protocol.py',
    'scripts/reconcile_preparing_image.py',
    'scripts/recover_forwarder_native.py',
    'scripts/recovery_checkpoint.py',
    'scripts/recovery_coordinator.py',
    'scripts/recovery_diagnostics.py',
    'scripts/recovery_dispositions.py',
    'scripts/recovery_manifest.py',
    'scripts/recovery_snapshot.py',
    'scripts/restore_admission.py',
    'scripts/restore_media_egress.py',
    'scripts/restore_reviewed_session.py',
    'scripts/restore_stopped_forwarder.py',
    'scripts/restore_storage_core.py',
    'scripts/restore_valid_previous.py',
    'scripts/resume_quarantined_native.py',
    'scripts/review_forwarder_outage.py',
    'scripts/review_stopped_send_worker.py',
    'scripts/run_bridge_trial.py',
    'scripts/run_forwarder.py',
    'scripts/run_group_probe.py',
    'scripts/run_login_probe.py',
    'scripts/run_onebot_tests.py',
    'scripts/run_private_forwarder.py',
    'scripts/runtime_manifest.py',
    'scripts/send_dispositions.py',
    'scripts/session_resume_protocol.py',
    'scripts/session_runtime.py',
    'scripts/session_runtime_client.py',
    'scripts/session_runtime_host.py',
    'scripts/session_state_store.py',
    'scripts/sticker-replay.py',
    'scripts/text_send_recovery.py',
    'scripts/verify_group_names.py',
    'scripts/voice_forwarding.py',
    'scripts/voice_send_native.py',
    'scripts/worker_supervisor.py',
    'tests/test_architecture_foundation.py',
    'tests/test_contact_receive_protocol.py',
    'tests/test_contact_send_protocol.py',
    'tests/test_durable_quarantine.py',
    'tests/test_empty_client_probe.py',
    'tests/test_forwarder_alert.py',
    'tests/test_group_metadata_protocol.py',
    'tests/test_group_reader_client.py',
    'tests/test_group_receive_protocol.py',
    'tests/test_group_send_protocol.py',
    'tests/test_hd_export_handoff.py',
    'tests/test_hd_gateway.py',
    'tests/test_hd_image_files.py',
    'tests/test_hd_image_native.py',
    'tests/test_hd_input_wire.py',
    'tests/test_hd_resources.py',
    'tests/test_image_send_recovery.py',
    'tests/test_login_client_probe.py',
    'tests/test_maintain_media_cache.py',
    'tests/test_media_cache_maintenance.py',
    'tests/test_media_capacity_contract.py',
    'tests/test_media_maintenance_policy.py',
    'tests/test_media_maintenance_snapshot.py',
    'tests/test_native_health_probe.py',
    'tests/test_native_migration.py',
    'tests/test_native_release.py',
    'tests/test_onebot_action_transport.py',
    'tests/test_onebot_all_groups.py',
    'tests/test_onebot_boot_restore.py',
    'tests/test_onebot_bridge.py',
    'tests/test_onebot_context.py',
    'tests/test_onebot_continuous.py',
    'tests/test_onebot_continuous_wire.py',
    'tests/test_onebot_forwarder_pipe.py',
    'tests/test_onebot_image_output.py',
    'tests/test_onebot_image_output_budget.py',
    'tests/test_onebot_image_output_native.py',
    'tests/test_onebot_image_source_cache.py',
    'tests/test_onebot_images.py',
    'tests/test_onebot_ipc.py',
    'tests/test_onebot_links.py',
    'tests/test_onebot_loop_guard.py',
    'tests/test_onebot_media_egress.py',
    'tests/test_onebot_mentions.py',
    'tests/test_onebot_native_forward.py',
    'tests/test_onebot_output_policy.py',
    'tests/test_onebot_routing.py',
    'tests/test_onebot_sender_names.py',
    'tests/test_onebot_services.py',
    'tests/test_onebot_simulation.py',
    'tests/test_onebot_sticker_probe.py',
    'tests/test_onebot_voice_native.py',
    'tests/test_onebot_voice_output.py',
    'tests/test_onebot_voice_relay.py',
    'tests/test_owned_netns_probe.py',
    'tests/test_prepare_network_modules.py',
    'tests/test_project_layout.py',
    'tests/test_qr_login_protocol.py',
    'tests/test_recovery_checkpoint.py',
    'tests/test_recovery_coordinator.py',
    'tests/test_recovery_dedup.py',
    'tests/test_recovery_diagnostics.py',
    'tests/test_recovery_disposition_integration.py',
    'tests/test_recovery_lifecycle.py',
    'tests/test_recovery_manifest.py',
    'tests/test_recovery_migration.py',
    'tests/test_recovery_receive_integration.py',
    'tests/test_recovery_snapshot.py',
    'tests/test_restore_admission.py',
    'tests/test_run_group_probe.py',
    'tests/test_run_login_probe.py',
    'tests/test_send_dispositions.py',
    'tests/test_session_resume_protocol.py',
    'tests/test_session_runtime.py',
    'tests/test_session_runtime_client.py',
    'tests/test_session_runtime_host.py',
    'tests/test_session_state_store.py',
    'tests/test_slirp_tap_probe.py',
    'tests/test_text_projection_wait.py',
    'tests/test_text_send_recovery.py',
    'tests/test_wechat_adapter.py',
    'tests/test_worker_supervisor.py',
    'tools/audit_publication.py',
    'tools/run_isolated_lifecycles.py',
    'tools/sync_runtime.py',
    'wechat_bridge/__init__.py',
    'wechat_bridge/adapters/__init__.py',
    'wechat_bridge/adapters/wechat/__init__.py',
    'wechat_bridge/adapters/wechat/assembly.py',
    'wechat_bridge/adapters/wechat/config.py',
    'wechat_bridge/adapters/wechat/legacy.py',
    'wechat_bridge/adapters/wechat/messages.py',
    'wechat_bridge/adapters/wechat/native.py',
    'wechat_bridge/adapters/wechat/policy.py',
    'wechat_bridge/adapters/wechat/proxy.py',
    'wechat_bridge/adapters/wechat/receipts.py',
    'wechat_bridge/adapters/wechat/sse.py',
    'wechat_bridge/continuous.py',
    'wechat_bridge/core/__init__.py',
    'wechat_bridge/core/contracts.py',
    'wechat_bridge/core/delivery.py',
    'wechat_bridge/core/messages.py',
    'wechat_bridge/core/sending.py',
    'wechat_bridge/core/text.py',
    'wechat_bridge/durable.py',
    'wechat_bridge/forward_config.py',
    'wechat_bridge/forward_gateway.py',
    'wechat_bridge/forward_wire.py',
    'wechat_bridge/gateway.py',
    'wechat_bridge/group_trigger.py',
    'wechat_bridge/image_input.py',
    'wechat_bridge/image_input_files.py',
    'wechat_bridge/image_input_wire.py',
    'wechat_bridge/image_output.py',
    'wechat_bridge/ipc.py',
    'wechat_bridge/loop_guard.py',
    'wechat_bridge/onebot/__init__.py',
    'wechat_bridge/onebot/actions.py',
    'wechat_bridge/onebot/codec.py',
    'wechat_bridge/onebot/content.py',
    'wechat_bridge/onebot/events.py',
    'wechat_bridge/onebot/registry.py',
    'wechat_bridge/onebot/service.py',
    'wechat_bridge/protocol.py',
    'wechat_bridge/sender_names.py',
    'wechat_bridge/simulation.py',
    'wechat_bridge/state.py',
    'wechat_bridge/storage/__init__.py',
    'wechat_bridge/storage/durable.py',
    'wechat_bridge/storage/state.py',
    'wechat_bridge/transport.py',
    'wechat_bridge/voice_output.py',
)

NATIVE_SOURCES = {
    'wechat_native.py': 'wechat_bridge/adapters/wechat/native.py',
    'wechat_sse.py': 'wechat_bridge/adapters/wechat/sse.py',
    'group_reader_client.py': 'scripts/group_reader_client.py',
    'group_receive_protocol.py': 'scripts/group_receive_protocol.py',
    'contact_receive_protocol.py': 'scripts/contact_receive_protocol.py',
    'group_metadata_protocol.py': 'scripts/group_metadata_protocol.py',
    'contact_send_protocol.py': 'scripts/contact_send_protocol.py',
    'group_send_protocol.py': 'scripts/group_send_protocol.py',
    'bridge_native_protocol.py': 'scripts/bridge_native_protocol.py',
    'forward_native_worker.py': 'scripts/forward_native_worker.py',
    'forward_sse.py': 'scripts/forward_sse.py',
    'forward_wire.py': 'wechat_bridge/forward_wire.py',
    'group_trigger.py': 'wechat_bridge/group_trigger.py',
    'loop_guard.py': 'wechat_bridge/loop_guard.py',
    'image_native.py': 'scripts/image_native.py',
    'image_input.py': 'wechat_bridge/image_input.py',
    'image_input_wire.py': 'wechat_bridge/image_input_wire.py',
    'image_output.py': 'wechat_bridge/image_output.py',
    'image_send_native.py': 'scripts/image_send_native.py',
    'voice_output.py': 'wechat_bridge/voice_output.py',
    'voice_send_native.py': 'scripts/voice_send_native.py',
    'sender_names.py': 'wechat_bridge/sender_names.py',
    'runtime_manifest.py': 'scripts/runtime_manifest.py',
}
HOST_SCRIPTS = ('scripts/bridge_host_worker.py', 'scripts/forward_host_worker.py',
                'scripts/runtime_manifest.py')


def validate_relative(relative):
    if not isinstance(relative, str):
        raise ValueError('public_code_path')
    path = PurePosixPath(relative)
    if (path.is_absolute() or path.as_posix() != relative or
            any(part.startswith('.') or part in {'data', 'state', 'accounts', 'checkpoints', 'run',
                'venv', 'onebot-env', 'private'} or 'staging' in part or part.endswith('-release')
                for part in path.parts) or path.suffix != '.py' or
            not (relative == 'run_onebot_tests.py' or len(path.parts) >= 2 and
                 path.parts[0] in {'scripts', 'tests', 'tools', 'wechat_bridge'})):
        raise ValueError('public_code_path')
    return path


def checked_path(root, relative, *, error='source_symlink'):
    validate_relative(relative)
    path = Path(root)
    if path.is_symlink():
        raise ValueError(error)
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(error)
    return path


def public_sources(root):
    result = {}
    for relative in sorted(PUBLIC_CODE):
        path = checked_path(root, relative)
        if not path.is_file():
            raise ValueError('public_code_missing')
        result[relative] = path
    result['run_onebot_tests.py'] = checked_path(root, 'scripts/run_onebot_tests.py')
    return dict(sorted(result.items()))


def role_files(role):
    if role == 'native':
        return tuple(NATIVE_SOURCES)
    if role == 'host':
        return tuple(sorted((*HOST_SCRIPTS, *(p for p in PUBLIC_CODE if p.startswith('wechat_bridge/')))))
    raise ValueError('worker_role')


def source_for(root, role, destination):
    if destination not in role_files(role):
        raise ValueError('worker_role_file')
    relative = NATIVE_SOURCES[destination] if role == 'native' else destination
    return checked_path(root, relative)


def verify_role(root, role):
    """Presence/type boundary; workers separately prove read-only mounts/ownership."""
    for name in role_files(role):
        relative = name if role == 'host' else 'scripts/' + name
        # Native destinations are flat; checked_path validates all parent links.
        path = checked_path(root, relative, error='runtime_symlink') if role == 'host' else Path(root) / name
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('worker_role_file')
    return True
