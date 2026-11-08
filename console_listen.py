# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Compressed (Opus / WebRTC) alternative to a plugin's L16 listen stream (`GET :8082/listen.pcm`).

The gateway does the work (cf. script.py, LISTEN_RELAY_*): a WHEP reader on the path
`cl-<a>-<b>-<c>-<d>-<bus>` makes MediaMTX start ONE ffmpeg that reads the console's /listen.pcm and
publishes Opus, shared by every reader of that (console, bus), stopped 10 s after the last one left.
This module only:
  - says whether the alternative is AVAILABLE for a console, and why not (codes, never sentences);
  - relays the WHEP signalling (SDP offer/answer, PATCH, DELETE) through the orchestrator, so the
    page talks to its own origin (no mixed content under HTTPS, no CORS) and the SAME rights as the
    L16 stream apply (`control.streams` entry of /listen.pcm, app/plugin_streams). Media (ICE/UDP)
    flows browser <-> gateway directly; nothing heavy passes through here.

Docs: docs/chantiers/CONSOLE_ECOUTE_WEBRTC.md (2026-10-16)."""
import ipaddress
import re
import secrets
import threading
import time

import requests

LISTEN_STREAM = "/listen.pcm"
L16_STEREO = "audio/L16;rate=48000;channels=2"
PATH_PREFIX = "cl"
BUS_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
CODEC = {"codec": "opus", "bitrate_kbps": 96, "frame_ms": 10, "channels": 2, "sample_rate": 48000}

GATEWAY_CACHE_S = 10.0          # live state of the gateway (:8080) re-read at most this often
SESSION_TTL_S = 12 * 3600       # forgotten after this (a WHEP session lives as long as its ICE)
SESSION_MAX = 512

_gw_cache = {"t": 0.0, "ip": None, "live": None}
_sessions = {}                  # token -> {"url", "vmid", "user", "t"}
_lock = threading.Lock()


def relay_path(ip, bus):
    """Gateway path of (console ip, bus), or None when either is not acceptable. Mirrors the regex
    of the gateway (script.py:LISTEN_RELAY_PATH_RE): an IPv4 and a bus id, nothing else."""
    try:
        a = ipaddress.IPv4Address(str(ip or ""))
    except ValueError:
        return None
    if not BUS_RE.fullmatch(str(bus or "")):
        return None
    return "%s-%s-%s" % (PATH_PREFIX, str(a).replace(".", "-"), bus)


def _settings():
    from app import settings as st
    return st


def gateway_live(force=False, *, http_get=None, now=None):
    """(ip, live :8080 dict or None) of the gateway, cached GATEWAY_CACHE_S."""
    st = _settings()
    ip = st.get("webrtc_gateway_ip") or None
    if not (st.get("webrtc_enabled") and ip):
        return None, None
    now = time.monotonic() if now is None else now
    with _lock:
        if (not force and _gw_cache["ip"] == ip and _gw_cache["live"] is not None
                and now - _gw_cache["t"] < GATEWAY_CACHE_S):
            return ip, _gw_cache["live"]
    live = None
    try:
        r = (http_get or requests.get)("http://%s:8080/" % ip, timeout=1.5)
        live = r.json() if r.status_code == 200 else None
    except Exception:
        live = None
    with _lock:
        _gw_cache.update(t=now, ip=ip, live=live)
    return ip, live


def gateway_state(force=False, **kw):
    """Code of the gateway's ability to relay: None (ready) or an error code."""
    ip, live = gateway_live(force, **kw)
    if not ip:
        return None, "gateway_not_deployed"
    if not live:
        return ip, "gateway_unreachable"
    if not live.get("running"):
        return ip, "gateway_not_running"
    if not live.get("listen_relay"):
        return ip, "gateway_no_relay"          # image without ffmpeg/libopus, or script < 0.4.0
    return ip, None


def listen_entry(manifest):
    """The plugin's declared L16 listen stream (control.streams), or None: only a plugin that serves
    /listen.pcm in the documented format can be relayed (the gateway command assumes it)."""
    from app import plugin_streams
    e = plugin_streams.find(manifest, "GET", LISTEN_STREAM)
    if not e:
        return None
    ct = str(e.get("content_type") or "").replace(" ", "").lower()
    return e if ct == L16_STEREO.lower() else None


def deny(vmid):
    """Flask refusal or None: the SAME rights as the L16 stream (its `control.streams` entry, checked
    by app/plugin_streams — project access viewer for `perm: read`, operator + plugins.operate for
    `perm: operate`). A plugin without the stream gets None here and `no_listen_stream` later."""
    from app import plugins, plugin_streams
    from app.database import db_get_container
    import json as _json
    c = db_get_container(vmid)
    if not c:
        return None
    dc = c.get("deploy_config") or {}
    if isinstance(dc, str):
        try:
            dc = _json.loads(dc)
        except ValueError:
            dc = {}
    e = listen_entry(plugins.get((dc or {}).get("type")) or {})
    return plugin_streams._deny(vmid, e) if e else None


def _console_listens(ip, *, http_get=None):
    """(default bus id, {bus id: label}) read from the console's /state, or (None, None)."""
    try:
        r = (http_get or requests.get)("http://%s:8082/state" % ip, timeout=2)
        s = r.json() if r.status_code == 200 else {}
    except Exception:
        return None, None
    first = (s.get("listen") or {}).get("bus")
    listens = {k: (v or {}).get("label") or k for k, v in (s.get("listens") or {}).items()}
    if first and first not in listens:
        listens[first] = (s.get("listen") or {}).get("label") or first
    return first, listens


def resolve(vmid, bus=None, *, http_get=None):
    """Everything the interface needs to choose L16 or WebRTC for console `vmid` (no side effect).
    Returns (http status, payload). `available` false always comes with a `reason` code."""
    from app import plugins
    from app.addressing import get_container_ip
    from app.database import db_get_container
    import json as _json
    c = db_get_container(vmid)
    if not c:
        return 404, {"available": False, "reason": "unknown_container"}
    dc = c.get("deploy_config") or {}
    if isinstance(dc, str):
        try:
            dc = _json.loads(dc)
        except ValueError:
            dc = {}
    m = plugins.get((dc or {}).get("type"))
    if not m or not listen_entry(m):
        return 400, {"available": False, "reason": "no_listen_stream"}
    out = {"available": False, "reason": None, "vmid": vmid, "bus": None, "buses": None,
           "transport": "webrtc", "signalling": "whep",
           "whep_url": "/api/webrtc/console-listen/%d/whep" % vmid, **CODEC}
    gw_ip, why = gateway_state(http_get=http_get)
    if why:
        out["reason"] = why
        return 200, out
    ip = get_container_ip(vmid)
    if not ip:
        out["reason"] = "console_no_ip"
        return 200, out
    first, listens = _console_listens(ip, http_get=http_get)
    if listens is None:
        out["reason"] = "console_unreachable"
        return 200, out
    out["buses"] = listens
    b = bus or first
    if not b or b not in listens:
        out["reason"] = "no_listen_bus"
        out["bus"] = bus
        return 200, out
    path = relay_path(ip, b)
    if not path:
        out["reason"] = "bad_bus"
        return 200, out
    out.update(available=True, bus=b, whep_url=out["whep_url"] + "?bus=" + b, path=path)
    out["_gateway"] = gw_ip           # stripped by the route; used by offer()
    return 200, out


def _purge(now):
    for k in [k for k, v in _sessions.items() if now - v["t"] > SESSION_TTL_S]:
        _sessions.pop(k, None)
    while len(_sessions) >= SESSION_MAX:
        _sessions.pop(min(_sessions, key=lambda k: _sessions[k]["t"]), None)


def offer(vmid, bus, sdp, user=None, *, http_get=None, http_post=None):
    """Relays a WHEP offer to the gateway. Returns (status, body, headers). On 201 the gateway's
    session URL is kept server-side and the caller receives OUR session URL in `Location`."""
    code, info = resolve(vmid, bus, http_get=http_get)
    if not info.get("available"):
        return (code if code != 200 else 409), {"error": info.get("reason")}, {}
    if not sdp or not str(sdp).lstrip().startswith("v=0"):
        return 400, {"error": "sdp_offer_expected"}, {}
    port = int(_settings().get("webrtc_http_port") or 8889)
    base = "http://%s:%d" % (info["_gateway"], port)
    url = "%s/%s/whep" % (base, info["path"])
    try:
        r = (http_post or requests.post)(url, data=str(sdp).encode(), timeout=12,
                                          headers={"Content-Type": "application/sdp"})
    except Exception:
        return 502, {"error": "gateway_unreachable"}, {}
    if r.status_code != 201:
        # 404 from MediaMTX = the relay could not start (console refused: too_many_listeners, bus
        # gone, console down) within runOnDemandStartTimeout.
        return 502, {"error": "relay_failed", "gateway_status": r.status_code}, {}
    loc = r.headers.get("Location") or ""
    if loc.startswith("/"):
        loc = base + loc
    token = secrets.token_urlsafe(18)
    now = time.monotonic()
    with _lock:
        _purge(now)
        _sessions[token] = {"url": loc, "vmid": vmid, "user": user, "t": now}
    headers = {"Location": "/api/webrtc/console-listen/session/" + token,
               "Content-Type": "application/sdp"}
    for h in ("ETag", "Link", "Accept-Patch"):
        if r.headers.get(h):
            headers[h] = r.headers[h]
    return 201, r.content, headers


def session(token, method, body=b"", content_type=None, user=None, *, http_request=None):
    """PATCH (trickle ICE / ICE restart) or DELETE (end) of a session opened by offer()."""
    with _lock:
        s = _sessions.get(token)
    if not s or not s["url"]:
        return 404, {"error": "unknown_session"}, {}
    if s.get("user") is not None and user is not None and s["user"] != user:
        return 403, {"error": "not_your_session"}, {}
    headers = {"Content-Type": content_type} if content_type else {}
    try:
        r = (http_request or requests.request)(method, s["url"], data=body or None,
                                                headers=headers, timeout=5)
    except Exception:
        if method == "DELETE":
            with _lock:
                _sessions.pop(token, None)
        return 502, {"error": "gateway_unreachable"}, {}
    if method == "DELETE":
        with _lock:
            _sessions.pop(token, None)
    else:
        with _lock:
            if token in _sessions:
                _sessions[token]["t"] = time.monotonic()
    out_h = {}
    if r.headers.get("Content-Type"):
        out_h["Content-Type"] = r.headers["Content-Type"]
    return r.status_code, r.content, out_h
