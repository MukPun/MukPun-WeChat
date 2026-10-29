# SoVITS to native WeChat voice

The continuous OneBot bridge accepts one `record` segment, optionally with a same-route reply. AstrBot serializes Record audio to base64 before sending the OneBot action. Accept only inline base64 PCM WAV; all file paths and URLs are rejected, with no new mounts or filesystem permissions.

Normalize bounded PCM WAV (8/16/24/32-bit integer, mono/stereo, 8–96 kHz) to mono PCM16 at 16 kHz using Python 3.12's audioop. The native v1.2.0 WAV fast path avoids an external FFmpeg dependency. Inputs are bounded to 8 MiB of inline OneBot data and 300 seconds. Normalized output is at most 9,600,044 bytes. Native splits clips longer than 60 seconds into the fewest equal-duration voice notes.

Use separately typed voice IPC frames, 64 KiB acknowledged chunks, a persistent native-send audit and the existing durable OneBot submission reservation. Upload exactly once and create exactly one idempotent `kind: voice` job. Never retry uncertain sends. Success requires all voice chunks to be accepted, matching server receipts and outgoing type-34 projections with available SILK/WAV assets. An unfinished or unknown voice audit prevents bridge startup. Existing text/image behavior, image limits and held image records are preserved.

Add `voiceOutputEnabled` (backward-compatible default false), capability reporting and minimal operational counters. Enabling voice is authorized by the feature request. Apply by restarting only this bridge after synthetic tests, review and an idle/no-pending check; reuse the existing Native process and login. No real voice message or model invocation is part of verification.
