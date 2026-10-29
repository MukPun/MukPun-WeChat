# Native voice implementation plan

> Execute with test-driven development and fresh verification before completion.

**Goal:** AstrBot SoVITS Record outputs become native WeChat voice notes.

**Architecture:** bounded WAV normalization in the unprivileged host worker; typed acknowledged IPC; root-owned audit and shared storage headroom; pinned Native media upload/send API with voice-specific receipt validation.

**Tech:** Python 3.12, stdlib WAV/audioop, existing OneBot and systemd isolation, agent-wechat 1.2.0.

**Constraints:** preserve Native/login and existing unknown image; never send real diagnostic media; no arbitrary file/network sources; preserve image 8 MiB limit; no public deployment evidence.

1. Add failing `tests/test_onebot_voice_output.py` for PCM normalization, inline-only source allowlist, malformed/truncated/overlong WAV, metadata/chunk integrity, capability, action deduplication and route checks. Implement `wechat_bridge/voice_output.py` and bridge/gateway integration. Run targeted tests to red then green.
2. Add failing `tests/test_onebot_voice_native.py` for upload binding, one-time submission, accepted/all-chunk receipt and type-34 asset projection, partial/unknown failure. Factor only the necessary source hooks in `scripts/image_send_native.py`; implement `scripts/voice_send_native.py`, leaving image receipts unchanged. Run voice and existing image native tests.
3. Add relay/isolation tests; implement `scripts/voice_forwarding.py`, voice worker dispatch, unchanged host isolation and backward-compatible configuration. Include strict unfinished-audit preflight, resource reservation and no interleaved actions. Run cross-layer tests.
4. Review source diff and independent code review. Run full unit tests and restricted Linux simulations, publication audit. Update public usage docs with contracts and synthetic evidence only.
5. Publish reviewed source. Back up bridge config/state, stop only the idle bridge, sync verified code, enable voice, restart bridge, verify readiness, original Native identity, intact held audits and no pending/unknown sends. Record deployment results privately; report actual verification limits to the user.
