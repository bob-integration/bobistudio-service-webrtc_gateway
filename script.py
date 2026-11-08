# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""webrtc_gateway launcher (passerelle WebRTC, déployé comme /opt/script/main.py par l'agent).

Ensures a MediaMTX binary is present (downloads the pinned release on first run if
absent), writes its config, launches + supervises it, and exposes the standard
:8080 metrics so the orchestrator's monitoring/agent contract holds.

MediaMTX provides: RTSP/WHIP ingest, WHEP playout, and a built-in browser player
page per path (http://<ip>:<http_port>/<path>) embedded as the Streams preview.

New-code naming in English by request (CLAUDE.md French convention overridden)."""
import hashlib, json, os, signal, subprocess, threading, time, urllib.request, tarfile, shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen

# ─── Config injectée (contrat plugin) ───────────────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"
RTSP_PORT   = int(CONFIG.get("rtsp_port") or 8554)
HTTP_PORT   = int(CONFIG.get("http_port") or 8889)
API_PORT    = int(CONFIG.get("api_port") or 9997)
# ★ PORT DU MÉDIA WebRTC, DISTINCT DE LA SIGNALISATION (2026-10-28). `HTTP_PORT` ne porte que
# l'échange WHEP ; les paquets audio/vidéo passent par ICE sur CE port (UDP, et TCP en repli pour
# un réseau qui bloque l'UDP). Sans lui publié, en « réseau interne », la signalisation répond 200,
# l'encodeur publie, et toutes les sessions meurent sur « deadline exceeded while waiting
# connection » : un panneau noir derrière une chaîne qui a l'air parfaite (relevé sur la Z8).
UDP_PORT    = int(CONFIG.get("udp_port") or 8189)
# Adresses à ANNONCER dans les candidats ICE, en plus de celles des interfaces du conteneur. En
# « réseau interne », l'IP du conteneur est privée au nœud : annoncée seule, le navigateur tente
# de joindre une adresse qui n'existe pas pour lui. L'orchestrateur injecte ici l'adresse de
# l'hôte vue du site (cf. `app/addressing.resolution_adresse_navigateur`).
ADDITIONAL_HOSTS = [str(h).strip() for h in (CONFIG.get("additional_hosts") or []) if str(h).strip()]
# Version FIXÉE par le service (`services/webrtc_gateway.MEDIAMTX_VERSION`) et vérifiée par empreinte :
# une version absente de MTX_SHA256 est REFUSÉE (erreur sur :8080) plutôt que téléchargée à l'aveugle.
MTX_VERSION = str(CONFIG.get("mediamtx_version") or "1.21.1")
# Empreintes SHA-256 des archives linux_amd64 officielles (fichier checksums.sha256 de la release,
# recoupé par sha256sum de l'archive téléchargée le 2026-10-12).
MTX_SHA256 = {{
    "1.21.1": "653abc672a3e693f8d3b2717752492fdcfb8072291ec108d03d3dd857411b0ee",
}}
# Horodatage absolu (`useAbsoluteTimestamp`, MediaMTX ≥ 1.12.0) : la passerelle garde la date NTP que
# l'encodeur met dans ses SR RTCP au lieu de dater chaque paquet à son ARRIVÉE — c'est elle qui part
# dans les SR vers le navigateur, qui cale son et image dessus. ⚠ Sans SR de l'éditeur, MediaMTX
# JETTE les paquets (« received RTP packet without absolute time ») : n'activer que pour une
# ingestion RTSP par ffmpeg, qui émet un SR avec son premier paquet.
ABS_TS_MIN_VERSION = (1, 12, 0)
# Serveur MoQ (Media over QUIC) : apparu en 1.19.0 et ACTIF par défaut — il ouvrirait :8892/:8893 et
# fabriquerait un certificat auto-signé dans le dossier courant. Nous ne le servons pas : coupé.
MOQ_MIN_VERSION = (1, 19, 0)
# En-tête de la liste YAML des hôtes ICE supplémentaires (séquence par blocs : aucune accolade, donc
# rien à doubler dans ce gabarit `str.format`).
_hotes_ice_bloc = "webrtcAdditionalHosts:\n"
ABSOLUTE_TIMESTAMP = bool(CONFIG.get("absolute_timestamp"))

# ─── Listen relay (audio console monitoring, compressed) ──────────────────────────────────────────
# A console serves its listen bus as uncompressed L16 (`GET :8082/listen.pcm`, ~1.5 Mbit/s per
# listener). For a remote listener (4G, VPN) the gateway re-serves it as Opus over WebRTC: a WHEP
# reader on `cl-<a>-<b>-<c>-<d>-<bus>` makes MediaMTX start ONE ffmpeg per (console, bus), whatever
# the number of readers, and stop it RUNON_CLOSE_AFTER after the last one left (`runOnDemand`) —
# no reaper, no container. The console address is IN the path (regex groups), so the gateway needs
# no table pushed by the orchestrator; the command is fixed (port 8082, /listen.pcm), only digits and
# a bus id reach it: nothing a client sends can add an argument. Needs ffmpeg in the image
# (bobi-webrtc >= 0.6.1); without it the path is not declared and `listen_relay` is false on :8080.
# Measured 2026-10-16 (same command on the controller, console 1160): 57 ms median console→RTSP
# read-back (23-82), 98 kbit/s, ~6 % of one core.
LISTEN_RELAY = bool(CONFIG.get("listen_relay", True))
LISTEN_RELAY_BITRATE = str(CONFIG.get("listen_relay_bitrate") or "96k")
LISTEN_RELAY_PATH_RE = ("~^cl-(\\d{{1,3}})-(\\d{{1,3}})-(\\d{{1,3}})-(\\d{{1,3}})"
                        "-([A-Za-z0-9_]{{1,64}})$")
RUNON_CLOSE_AFTER = "10s"

BIN_PATH = "/usr/local/bin/mediamtx"
CFG_PATH = "/opt/mediamtx.yml"
DL_URL = f"https://github.com/bluenviron/mediamtx/releases/download/v{{MTX_VERSION}}/mediamtx_v{{MTX_VERSION}}_linux_amd64.tar.gz"

state = {{"running": False, "paths": [], "error": None, "version": MTX_VERSION,
         "absolute_timestamp": False, "listen_relay": False, "listen_relay_bitrate": None}}

# ─── Métriques :8080 (contrat agent) ─────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(state).encode())
    def log_message(self, *a): pass

threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(),
    daemon=True).start()


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split(".")[:3])
    except ValueError:
        return (0, 0, 0)


def installed_version():
    """Version du binaire présent (`mediamtx --version` → « v1.21.1 »), None si absent/illisible."""
    if not (os.path.exists(BIN_PATH) and os.access(BIN_PATH, os.X_OK)):
        return None
    try:
        out = subprocess.run([BIN_PATH, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().lstrip("v") or None
    except Exception:
        return None


def ensure_binary():
    """Retourne True si le binaire mediamtx de la version FIXÉE est prêt. Sinon le télécharge et
    vérifie son empreinte. Le binaire PRÉ-BAKÉ de l'image n'est gardé que s'il a la bonne version :
    une image plus ancienne (bobi-webrtc ≤ 0.5 : 1.9.3) ne doit pas imposer sa version en silence."""
    have = installed_version()
    if have == MTX_VERSION:
        return True
    expected = MTX_SHA256.get(MTX_VERSION)
    if not expected:
        state["error"] = (f"MediaMTX v{{MTX_VERSION}} : aucune empreinte connue — version refusée "
                          f"(versions vérifiables : {{', '.join(sorted(MTX_SHA256))}})")
        print(state["error"])
        return False
    try:
        print(f"MediaMTX présent : v{{have}} ; téléchargement de v{{MTX_VERSION}} depuis {{DL_URL}}")
        tmp = "/tmp/mediamtx.tar.gz"
        with urllib.request.urlopen(DL_URL, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            state["error"] = (f"MediaMTX v{{MTX_VERSION}} : empreinte SHA-256 inattendue "
                              f"({{h.hexdigest()}}) — archive rejetée")
            print(state["error"])
            os.remove(tmp)
            return False
        with tarfile.open(tmp, "r:gz") as tar:
            member = next((m for m in tar.getmembers() if os.path.basename(m.name) == "mediamtx"), None)
            if not member:
                state["error"] = "binaire mediamtx absent de l'archive"
                return False
            member.name = os.path.basename(member.name)
            if os.path.exists(BIN_PATH):
                os.remove(BIN_PATH)
            tar.extract(member, os.path.dirname(BIN_PATH), filter="data")
        os.chmod(BIN_PATH, 0o755)
        os.remove(tmp)
        print(f"MediaMTX v{{installed_version()}} installé (empreinte vérifiée)")
        return True
    except Exception as e:
        state["error"] = f"téléchargement MediaMTX échoué : {{e}} (le container a-t-il un accès Internet ?)"
        print(state["error"])
        return False


def relay_bitrate(v):
    """Opus bitrate for the listen relay: « <n>k », 32..256 kbit/s, else the 96k default (the value
    lands in a command line — only a validated token may reach it)."""
    s = str(v or "").strip().lower()
    if s.endswith("k") and s[:-1].isdigit() and 32 <= int(s[:-1]) <= 256:
        return s
    return "96k"


def listen_relay_cmd(bitrate):
    """`runOnDemand` command of the listen relay. $G1..$G5 (regex groups) and $RTSP_PORT / $MTX_PATH
    are expanded by MediaMTX itself, then split like a shell would — no shell involved.
    - input: the console's L16 stream, read from its live edge (no probing: the format is the
      contract of /listen.pcm, `-probesize 32` avoids buffering ~1 s before the first packet);
    - Opus `lowdelay` (CELT only, no SILK look-ahead), 10 ms frames, constrained VBR."""
    return ("ffmpeg -nostdin -hide_banner -loglevel warning -fflags nobuffer -probesize 32 "
            "-analyzeduration 0 -f s16le -ar 48000 -ac 2 "
            "-i http://$G1.$G2.$G3.$G4:8082/listen.pcm?bus=$G5 "
            "-c:a libopus -b:a %s -vbr constrained -application lowdelay -frame_duration 10 "
            "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH" % relay_bitrate(bitrate))


def ffmpeg_available():
    """True when an ffmpeg with libopus is in the image (bobi-webrtc >= 0.6.1)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
        return "libopus" in (out.stdout or "")
    except Exception:
        return False


def render_config(version, absolute_timestamp, listen_relay_bitrate=None,
                  udp_port=None, additional_hosts=()):
    """Configuration MediaMTX (YAML). Pure → testable hors ligne (tests/verif_webrtc_gateway_config.py).
    Clés vérifiées contre le mediamtx.yml de référence de la v1.21.1 : aucune n'a changé de nom
    depuis 1.9.3 (les renommages — *AllowOrigin → *AllowOrigins, protocols → rtspTransports,
    serverCert → rtspServerCert — portent sur des clés que nous ne posons pas).
    `listen_relay_bitrate` : None → no listen relay path ; else the on-demand Opus relay path is
    declared (cf. listen_relay_cmd). Absolute timestamps are OFF on it: audio only, nothing to align
    with, and MediaMTX would drop everything until the first RTCP SR (~5 s of silence at start)."""
    abs_ts = bool(absolute_timestamp) and _version_tuple(version) >= ABS_TS_MIN_VERSION
    _udp = int(udp_port or 8189)
    cfg = f"""# Généré par l'orchestrateur — passerelle WebRTC MXL (MediaMTX v{{version}})
logLevel: info
api: yes
apiAddress: :{{API_PORT}}
rtsp: yes
rtspAddress: :{{RTSP_PORT}}
rtmp: no
hls: no
srt: no
webrtc: yes
webrtcAddress: :{{HTTP_PORT}}
"""
    # Posé en substitution (et non dans la f-string) pour que `_udp` soit un usage LISIBLE : dans
    # ce gabarit `str.format`, un `{{_udp}}` n'est pas encore un placeholder, et pyflakes le
    # signalerait comme variable inutilisée à chaque passage.
    cfg += "webrtcLocalUDPAddress: :%d\nwebrtcLocalTCPAddress: :%d\n" % (_udp, _udp)
    # `webrtcIPsFromInterfaces` reste à son défaut (oui) : l'IP du conteneur garde sa place dans
    # les candidats, et c'est celle qui sert à l'orchestrateur, sur le nœud. On AJOUTE l'adresse du
    # site, on ne remplace pas — remplacer casserait la lecture locale pour réparer la distante.
    _hotes = [str(h).strip() for h in (additional_hosts or []) if str(h).strip()]
    if _hotes:
        cfg += _hotes_ice_bloc + "".join("  - %s\n" % _h for _h in _hotes)
    if _version_tuple(version) >= MOQ_MIN_VERSION:
        cfg += "moq: no\n"
    if abs_ts:
        cfg += """pathDefaults:
  useAbsoluteTimestamp: yes
"""
    cfg += "paths:\n"
    if listen_relay_bitrate:
        cfg += "  \"%s\":\n" % LISTEN_RELAY_PATH_RE.replace("\\", "\\\\")
        if abs_ts:
            cfg += "    useAbsoluteTimestamp: no\n"
        cfg += ("    runOnDemand: %s\n"
                "    runOnDemandRestart: yes\n"
                "    runOnDemandStartTimeout: 10s\n"
                "    runOnDemandCloseAfter: %s\n") % (listen_relay_cmd(listen_relay_bitrate), RUNON_CLOSE_AFTER)
    cfg += "  all_others:\n"
    return cfg, abs_ts


def write_config():
    version = installed_version() or MTX_VERSION
    relay = LISTEN_RELAY and ffmpeg_available()
    br = relay_bitrate(LISTEN_RELAY_BITRATE) if relay else None
    cfg, abs_ts = render_config(version, ABSOLUTE_TIMESTAMP, listen_relay_bitrate=br,
                                udp_port=UDP_PORT, additional_hosts=ADDITIONAL_HOSTS)
    with open(CFG_PATH, "w") as f:
        f.write(cfg)
    state["version"] = version
    state["absolute_timestamp"] = abs_ts
    state["listen_relay"] = bool(relay)
    state["listen_relay_bitrate"] = br
    if LISTEN_RELAY and not relay:
        print("listen relay unavailable: no ffmpeg with libopus in this image (bobi-webrtc >= 0.6.1 needed)")


def poll_paths(proc):
    """Boucle de rafraîchissement des métriques (paths actifs via l'API MediaMTX)."""
    while True:
        time.sleep(3)
        alive = proc.poll() is None
        state["running"] = alive
        if not alive:
            state["paths"] = []
            continue
        try:
            with urlopen(f"http://127.0.0.1:{{API_PORT}}/v3/paths/list", timeout=2) as r:
                data = json.loads(r.read().decode())
            state["paths"] = [it.get("name") for it in (data.get("items") or [])]
            state["error"] = None
        except Exception as e:
            state["paths"] = []


def main():
    if not ensure_binary():
        # Garde le process vivant pour que :8080 expose l'erreur (agent voit "running")
        while True:
            time.sleep(5)
    write_config()
    while True:
        print("Démarrage MediaMTX…")
        proc = subprocess.Popen([BIN_PATH, CFG_PATH])
        state["running"] = True
        state["error"] = None
        watcher = threading.Thread(target=poll_paths, args=(proc,), daemon=True)
        watcher.start()
        proc.wait()
        state["running"] = False
        print("MediaMTX arrêté, redémarrage dans 2s…")
        time.sleep(2)


if __name__ == "__main__":
    main()
