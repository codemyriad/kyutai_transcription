#!/usr/bin/env node
/**
 * Stream a local audio file into a Nextcloud Talk room using Node.js + wrtc.
 *
 * Env vars:
 *   ROOM_URL     Full Talk room URL (preferred, e.g. $NEXTCLOUD_ROOM_URL)
 *   NEXTCLOUD_ROOM_URL Optional alias for ROOM_URL
 *   BASE_URL     Base Nextcloud URL (fallback if ROOM_URL unset; NEXTCLOUD_URL is also checked)
 *   ROOM_TOKEN   Talk room token (fallback if ROOM_URL unset)
 *   AUDIO_FILE   Path to WAV/PCM audio to send
 *   NICKNAME     Optional display name (used in nickChanged/status messages)
 *
 * Requires: ffmpeg, npm i ws wrtc fluent-ffmpeg set-cookie-parser
 */

const fs = require('fs');
const path = require('path');
const setCookie = require('set-cookie-parser');
const WebSocket = require('ws');
const ffmpeg = require('fluent-ffmpeg');
const { RTCPeerConnection, RTCSessionDescription, nonstandard } = require('wrtc');

const { RTCAudioSource } = nonstandard;

const ROOM_URL = process.env.ROOM_URL || process.env.NEXTCLOUD_ROOM_URL;
const fallbackBase = process.env.BASE_URL || process.env.NEXTCLOUD_URL || 'https://example.com';
const fallbackRoom = process.env.ROOM_TOKEN || 'erwcr27x';
const audioCandidates = [
  process.env.AUDIO_FILE,
  path.resolve(__dirname, '../kyutai_modal/test_audio.wav'),
  path.resolve(__dirname, '../../kyutai_modal/test_audio.wav'),
].filter(Boolean);
const AUDIO = audioCandidates.find(f => fs.existsSync(f));
if (!AUDIO) {
  throw new Error('Audio file not found. Set AUDIO_FILE env or place test_audio.wav in ../kyutai_modal');
}
const NICK = process.env.NICKNAME || 'Bot';

const { baseUrl: BASE, roomToken: ROOM } = (() => {
  if (!ROOM_URL) {
    return { baseUrl: fallbackBase, roomToken: fallbackRoom };
  }
  const parsed = new URL(ROOM_URL);
  const segments = parsed.pathname.split('/').filter(Boolean);
  const token = segments.pop();
  return { baseUrl: `${parsed.protocol}//${parsed.host}`, roomToken: token };
})();

async function fetchWithCookies(url, options = {}, jar = { cookies: [] }) {
  const res = await fetch(url, options);
  const set = res.headers.getSetCookie ? res.headers.getSetCookie() : res.headers.raw?.()['set-cookie'];
  if (set && set.length) {
    jar.cookies = setCookie.splitCookiesString(set).map(c => setCookie.parseString(c));
  }
  const cookieHeader = jar.cookies.map(c => `${c.name}=${c.value}`).join('; ');
  return { res, cookieHeader };
}

async function loadRoomPage(jar) {
  const { res, cookieHeader } = await fetchWithCookies(`${BASE}/call/${ROOM}`, {}, jar);
  const html = await res.text();
  const m = html.match(/data-requesttoken="([^"]+)"/);
  if (!m) throw new Error('requesttoken not found');
  return { requesttoken: m[1], cookieHeader };
}

async function ocsPost(pathname, body, jar, requesttoken) {
  const { cookieHeader } = jar;
  const res = await fetch(`${BASE}${pathname}`, {
    method: 'POST',
    headers: {
      'OCS-APIREQUEST': 'true',
      'requesttoken': requesttoken,
      'Content-Type': 'application/json',
      'Cookie': cookieHeader,
    },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  if (!json.ocs) {
    throw new Error(`Unexpected OCS envelope for ${pathname}: ${JSON.stringify(json)}`);
  }
  return json.ocs.data ?? {};
}

async function ocsGet(pathname, params, jar, requesttoken) {
  const query = new URLSearchParams({ format: 'json', ...params });
  const res = await fetch(`${BASE}${pathname}?${query.toString()}`, {
    headers: {
      'OCS-APIREQUEST': 'true',
      ...(requesttoken ? { requesttoken } : {}),
      'Cookie': jar.cookieHeader,
    },
  });
  return res.json();
}

function buildIceServers(settings) {
  const servers = [];
  (settings.stunservers || []).forEach(s => servers.push({ urls: s.urls }));
  (settings.turnservers || []).forEach(t => servers.push({ urls: t.urls, username: t.username, credential: t.credential }));
  return servers;
}

function startPcmPump(audioSources, file) {
  const cmd = ffmpeg(file)
    .format('s16le')
    .audioChannels(1)
    .audioFrequency(48000)
    .audioCodec('pcm_s16le')
    .on('error', err => console.error('ffmpeg error', err.message));

  const proc = cmd.pipe();
  const chunkBytes = 960; // 10ms * 48kHz * 2 bytes per sample
  let buffer = Buffer.alloc(0);
  let ended = false;

  proc.on('data', buf => {
    buffer = Buffer.concat([buffer, buf]);
  });
  proc.on('end', () => {
    ended = true;
  });

  const interval = setInterval(() => {
    if (buffer.length >= chunkBytes) {
      const slice = buffer.subarray(0, chunkBytes);
      buffer = buffer.subarray(chunkBytes);
      const samples = new Int16Array(chunkBytes / 2);
      slice.copy(Buffer.from(samples.buffer));
      for (const source of audioSources) {
        try {
          source.onData({ samples, sampleRate: 48000 });
        } catch (err) {
          console.warn('Audio onData failed:', err.message);
        }
      }
    } else if (ended) {
      clearInterval(interval);
    }
  }, 10);

  return {
    stop() {
      clearInterval(interval);
      proc.destroy();
    },
  };
}

async function main() {
  const jar = {};
  const { requesttoken, cookieHeader } = await loadRoomPage(jar);
  jar.cookieHeader = cookieHeader;
  const participant = await ocsPost(
    `/ocs/v2.php/apps/spreed/api/v4/room/${ROOM}/participants/active?format=json`,
    { force: true },
    jar,
    requesttoken,
  );
  console.log('room sessionId (for join):', participant.sessionId);

  const settings = (await ocsGet(
    '/ocs/v2.php/apps/spreed/api/v3/signaling/settings',
    { token: ROOM },
    jar,
    requesttoken,
  )).ocs.data;
  const callJoin = await ocsPost(
    `/ocs/v2.php/apps/spreed/api/v4/call/${ROOM}?format=json`,
    { flags: 3, silent: false, recordingConsent: false, silentFor: [] },
    jar,
    requesttoken,
  );
  console.log('call join response', callJoin);

  const server = settings.server.replace(/^https/, 'wss').replace(/^http/, 'ws');
  const ws = new WebSocket(server.replace(/\/$/, '') + '/spreed');

  const audioSources = new Set();
  const connections = new Map(); // sid -> { pc, targetSessionId, source }
  let pcmPump = null;
  let signalingSessionId = null;
  const publishSid = `${Date.now()}`;

  const sendMessage = obj => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  };

  const sendSignal = (conn, type, payload) => {
    const recipient = conn.targetSessionId
      ? { type: 'session', sessionid: conn.targetSessionId }
      : { type: 'room' };
    const data = {
      to: conn.targetSessionId || '',
      sid: conn.sid,
      roomType: 'video',
      type,
      payload,
    };
    sendMessage({ type: 'message', message: { recipient, data } });
  };

  const createConnection = (sid, targetSessionId = null) => {
    if (connections.has(sid)) {
      const existing = connections.get(sid);
      existing.targetSessionId = targetSessionId || existing.targetSessionId;
      return existing;
    }

    const source = new RTCAudioSource();
    audioSources.add(source);
    const track = source.createTrack();
    track.enabled = true;
    const pc = new RTCPeerConnection({ iceServers: buildIceServers(settings) });
    const transceiver = pc.addTransceiver(track, {
      direction: 'sendonly',
      sendEncodings: [{ maxBitrate: 64000 }],
    });
    transceiver.sender.replaceTrack(track);

    pc.onicecandidate = ev => {
      if (!ev.candidate) {
        return;
      }
      sendSignal(
        conn,
        'candidate',
        {
          candidate: {
            candidate: ev.candidate.candidate,
            sdpMid: ev.candidate.sdpMid,
            sdpMLineIndex: ev.candidate.sdpMLineIndex,
          },
        },
      );
    };
    pc.onconnectionstatechange = () => {
      console.log(`[pc ${sid}] state=${pc.connectionState}`);
    };

    const conn = { pc, targetSessionId, sid, source };
    connections.set(sid, conn);
    if (!pcmPump) {
      pcmPump = startPcmPump(audioSources, AUDIO);
      console.log('PCM pump started from', AUDIO);
    }
    return conn;
  };

  const sendOffer = async conn => {
    const offer = await conn.pc.createOffer({ offerToReceiveAudio: false, offerToReceiveVideo: false });
    await conn.pc.setLocalDescription(offer);
    sendSignal(conn, 'offer', { type: 'offer', sdp: conn.pc.localDescription.sdp, nick: NICK });
    // Also broadcast nickname so UI shows something for us.
    sendMessage({
      type: 'message',
      message: {
        recipient: { type: 'room' },
        data: { to: '', roomType: 'video', type: 'nickChanged', payload: { name: NICK } },
      },
    });
  };

  const handleOffer = async (fromSessionId, data) => {
    const sid = data.sid || `sid-${Date.now()}`;
    const conn = createConnection(sid, fromSessionId);
    const sdp = data.payload?.sdp || data.payload?.offer || data.payload?.payload || data.payload;
    await conn.pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp }));
    const answer = await conn.pc.createAnswer();
    await conn.pc.setLocalDescription(answer);
    sendSignal(conn, 'answer', { type: 'answer', sdp: conn.pc.localDescription.sdp, nick: NICK });
  };

  const handleAnswer = async (fromSessionId, data) => {
    const sid = data.sid || publishSid;
    const conn = connections.get(sid);
    if (!conn) {
      console.warn('Answer for unknown sid', sid);
      return;
    }
    conn.targetSessionId = conn.targetSessionId || fromSessionId;
    const sdp = data.payload?.sdp || data.payload;
    await conn.pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp }));
    console.log(`[pc ${sid}] answer applied from ${fromSessionId}`);
  };

  const handleCandidate = async (fromSessionId, data) => {
    const sid = data.sid || publishSid;
    const conn = connections.get(sid);
    if (!conn) {
      console.warn('Candidate for unknown sid', sid);
      return;
    }
    conn.targetSessionId = conn.targetSessionId || fromSessionId;
    const cand = data.payload?.candidate || data.payload;
    if (!cand || !cand.candidate) return;
    try {
      await conn.pc.addIceCandidate({
        candidate: cand.candidate,
        sdpMid: cand.sdpMid || data.sdpMid,
        sdpMLineIndex: cand.sdpMLineIndex ?? data.sdpMLineIndex,
      });
    } catch (err) {
      console.warn('Failed to apply candidate', err.message);
    }
  };

  let helloSent = false;
  const sendHello = () => {
    if (helloSent) return;
    helloSent = true;
    const helloVersion = settings.helloAuthParams['2.0'] ? '2.0' : '1.0';
    sendMessage({
      type: 'hello',
      hello: {
        version: helloVersion,
        auth: {
          url: `${BASE}/ocs/v2.php/apps/spreed/api/v3/signaling/backend`,
          params: settings.helloAuthParams[helloVersion],
        },
      },
    });
  };

  ws.on('open', () => {
    if (!settings.helloAuthParams['2.0']) {
      sendHello();
    } else {
      setTimeout(sendHello, 3000);
    }
  });

  ws.on('message', async data => {
    const msg = JSON.parse(data);
    if (msg.type === 'welcome') {
      sendHello();
      return;
    }
    if (msg.type === 'hello') {
      signalingSessionId = msg.hello.sessionid;
      console.log('Signaling session id', signalingSessionId);
      sendMessage({ type: 'room', room: { roomid: ROOM, sessionid: participant.sessionId } });
      return;
    }
    if (msg.type === 'room') {
      // Initial publish
      const conn = createConnection(publishSid, null);
      await sendOffer(conn);
      return;
    }
    if (msg.type === 'event') {
      // FYI only
      return;
    }
    if (msg.type === 'message') {
      const from = msg.message.sender?.sessionid || msg.message.data?.from;
      const d = msg.message.data;
      switch (d.type) {
        case 'offer':
          await handleOffer(from, d);
          break;
        case 'answer':
          await handleAnswer(from, d);
          break;
        case 'candidate':
          await handleCandidate(from, d);
          break;
        default:
          console.log('Unhandled message', d.type);
      }
    }
  });

  process.on('SIGINT', async () => {
    ws.close();
    for (const conn of connections.values()) {
      conn.pc.close();
    }
    pcmPump?.stop();
    process.exit(0);
  });
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
