# How Nextcloud Talk enables live transcription and ExApp UI integration

The CC button in Nextcloud Talk appears when the server advertises the **`config.call.live-transcription`** capability, which requires both a live transcription ExApp and a High-Performance Backend (HPB) signaling server. ExApps register capabilities through AppAPI's OCS endpoints during their enable handler, but Talk does **not** expose direct UI extension points for custom call buttons—ExApps integrate via Talk Bots, signaling messages, or backend APIs instead.

## Detecting live transcription capability

Talk's frontend conditionally renders the CC button by checking the `spreed` capabilities at the path **`config → call → live-transcription`**. This boolean capability indicates whether live transcription is supported in calls. The capability only appears when both prerequisites are met: an installed live transcription ExApp and a configured High-Performance Backend (external signaling server).

The frontend uses a composable pattern to check capabilities, following Talk's standard architecture:

```javascript
// Typical capability check in Talk's Vue components
const { hasTalkCapability } = useTalkCapabilities()
const isLiveTranscriptionAvailable = hasTalkCapability('config', 'call', 'live-transcription')
```

When this capability returns true, Talk renders transcription-related UI elements including language selection and the CC toggle button. Users opt into receiving transcriptions via a checkbox in the "media settings" modal, similar to the call recording consent flow.

## How ExApps register capabilities with Nextcloud

ExApps register their capabilities through **AppAPI's OCS API endpoints** during the app initialization lifecycle. The registration flow works as follows:

The ExApp container starts and responds to AppAPI's heartbeat checks. During initialization, it reports progress to Nextcloud via `POST /ocs/v1.php/apps/app_api/apps/status/{app_id}`. When fully initialized, the ExApp receives a PUT request to its `/enabled` endpoint. Inside the `enabled_handler`, the ExApp registers its providers through specific OCS endpoints:

| Provider Type | Register Endpoint |
|--------------|-------------------|
| Speech-To-Text | `POST /apps/app_api/api/v1/speech_to_text` |
| Text Processing | `POST /apps/app_api/api/v1/text_processing` |
| Translation | `POST /apps/app_api/api/v1/translation` |
| Talk Bot | `POST /apps/app_api/api/v1/talk_bot` |

For Python-based ExApps using the `nc_py_api` library, registration looks like this:

```python
def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    if enabled:
        nc.providers.speech_to_text.register(
            name="Whisper STT",
            display_name="Local Whisper Speech-To-Text",
            callback_url="/transcribe"
        )
    return ""
```

Registered capabilities are then exposed through Nextcloud's standard **OCS capabilities endpoint** at `GET /ocs/v2.php/cloud/capabilities`, which Talk and other apps query to discover available features.

## Talk's frontend capability checking architecture

Talk retrieves capabilities from the OCS capabilities endpoint and stores them in the browser. The capabilities are structured hierarchically under the `spreed` namespace, with transcription-related capabilities at **`spreed.config.call`** level. Key capability paths include:

- `config.call.live-transcription` – Whether real-time transcription is supported
- `config.chat.has-translation-task-providers` – Whether message translation is available  
- `config.call.recording` – Whether call recording is available

The frontend uses **Vue composables** (like `useTalkCapabilities`) to abstract capability checking, enabling reactive UI updates when capabilities change. Components conditionally render features using computed properties that evaluate these capability checks.

For federated conversations, Talk handles capability differences between local and remote servers, storing remote capabilities separately and checking both when determining available features.

## ExApp UI integration options: no direct call UI slots exist

**Talk does not expose dedicated extension points for ExApps to add custom buttons within the call interface.** The call UI components are tightly integrated, and there's no slot/hook system for injecting third-party controls. However, ExApps can integrate with Talk through several alternative mechanisms:

**Talk Bots** are the primary integration path, registered via `POST /apps/app_api/api/v1/talk_bot`. Bots receive webhooks for chat messages and can respond programmatically. While bots operate through chat rather than call controls, they can trigger actions based on commands.

**AppAPI UI Extensions** provide general Nextcloud UI integration points including top menu entries (`POST /apps/app_api/api/v1/ui/top-menu`), file action menus (`POST /apps/app_api/api/v2/ui/files-actions-menu`), and declarative settings (`POST /apps/app_api/api/v1/ui/settings`). These don't extend Talk's call UI specifically but allow ExApps to create their own UI surfaces.

**Signaling messages** enable real-time communication during calls. The live transcription ExApp sends transcriptions directly through the signaling server to participants who opted in, bypassing Talk's UI layer entirely. The message format is straightforward:

```json
{
    "sessionId": "participant_session_id",
    "transcriptionMessage": "transcribed text content"
}
```

## Architecture of Talk's transcription integration

Talk implements two distinct transcription architectures serving different use cases:

### Batch transcription for call recordings

When call recording completes, Talk triggers the speech-to-text provider through Nextcloud's **`ISpeechToTextManager`** interface. The provider processes the recorded file asynchronously and emits completion events. Talk then notifies moderators that the transcript is available for sharing. Configuration is controlled via `occ config:app:set spreed call_recording_transcription --value yes`.

### Live transcription during calls

Live transcription follows a different architecture that bypasses the standard provider APIs. The live transcription ExApp:

1. Connects directly to the **HPB signaling server** and authenticates
2. Receives the participant list and audio streams via WebRTC
3. Transcodes audio streams and feeds them to a transcription engine (typically Whisper)
4. Sends transcription messages directly through signaling to opted-in participants

This architecture requires the HPB because the browser-based signaling can't handle the audio stream processing needed for real-time transcription. When a user enables live transcription, Talk's backend sends a request to the ExApp's `/transcribeCall` endpoint with `roomToken` and `sessionId` parameters.

The **stt_whisper2** ExApp exemplifies this pattern—it's a Python-based Docker container using faster-whisper for GPU-accelerated transcription. It registers as a speech-to-text provider during initialization and handles both batch and live transcription workloads.

## Conclusion

Nextcloud Talk's transcription capability detection relies on a well-defined capability path (`config.call.live-transcription`) that the frontend checks before rendering the CC button. ExApps integrate through AppAPI's OCS endpoints rather than Talk-specific extension points, and the live transcription architecture notably bypasses standard provider APIs in favor of direct signaling server integration. While ExApps cannot inject custom buttons into Talk's call interface, they can leverage Talk Bots, signaling messages, and general AppAPI UI extensions to create complementary experiences. The requirement for an HPB signaling server represents a significant infrastructure prerequisite for live transcription functionality.