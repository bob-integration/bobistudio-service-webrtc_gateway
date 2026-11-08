# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""WebRTC gateway management : deploy + configure a dedicated MediaMTX container.

The gateway is a normal managed container (cloned from the base template, like any
other) running the `webrtc_gateway` script. Encoders push their WebRTC-destined
stream to it (RTSP/WHIP ingest) and the Streams page embeds its WHEP player page.

New-code naming in English by request (CLAUDE.md French convention overridden)."""
import logging
import os
import threading

import requests

from app import settings as st
from app.database import db_set_setting, db_get_container, db_get_containers, db_get_node
from app.deploy import deployer_script

_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "script.py")
_TYPE        = "webrtc_gateway"

# Version de MediaMTX FIXÉE ici (dernière stable publiée depuis au moins deux semaines au 2026-10-12 ;
# la 1.21.2 du 2026-10-08 était trop fraîche). Le script refuse toute version dont il ne connaît pas
# l'empreinte SHA-256 (script.py:MTX_SHA256) : changer de version = mettre à jour les DEUX.
# Le réglage `webrtc_mediamtx_version` n'est plus qu'un affichage : une version différente est
# refusée par /api/webrtc/apply au lieu d'être déployée sans vérification.
MEDIAMTX_VERSION = "1.21.1"


def _render_script(params, hostname):
    """Rend le script MediaMTX avec les paramètres injectés."""
    with open(_SCRIPT_PATH) as f:
        tpl = f.read()
    return tpl.format(config=repr(params), hostname=hostname, plugin_version="1.0.0")


def render_script(params, hostname):
    """Point d'entrée PUBLIC du rendu, résolu par convention depuis `app.scripts.generer_script`.

    Le service passe normalement son script en `script_content` à `deployer_script`. Mais les
    chemins qui redéploient SANS contenu — auto-réparation « script perdu » après un reboot,
    reprise de nœud, re-provision des certificats mTLS — passent par `generer_script`. Sans cette
    fonction, la passerelle échoue sur « Type de script inconnu » et reste morte alors que tout le
    reste du nœud est relevé (constaté sur Horace le 2026-07-28)."""
    return _render_script(params or {}, hostname or "webrtc-gateway")

log = logging.getLogger(__name__)


def _noeud_de_la_passerelle(node_id=None):
    """Le nœud qui héberge la passerelle : celui qu'on déploie, sinon celui du conteneur en base.

    `node_id` est passé au DÉPLOIEMENT, où la passerelle n'a pas encore de ligne en base (ou en a
    une sur un autre nœud) : s'en remettre aux réglages produirait la configuration du nœud
    PRÉCÉDENT, c'est-à-dire la panne qu'on répare, déplacée.
    """
    from app.database import db_get_container, db_get_node
    if node_id:
        return db_get_node(int(node_id))
    vmid = int(st.get("webrtc_gateway_vmid") or 0)
    c = db_get_container(vmid) if vmid else None
    if c and c.get("node_id"):
        return db_get_node(c["node_id"])
    nid = int(st.get("webrtc_node_id") or 0)
    return db_get_node(nid) if nid else None


def _hotes_ice(node_id=None):
    """Adresses à ANNONCER dans les candidats ICE, en plus des interfaces du conteneur.

    ★ LE MÉDIA N'EST PAS LA SIGNALISATION (2026-10-28, Z8). En « réseau interne », publier le port
    de signalisation suffit à obtenir un 200 et une session... qui meurt sur « deadline exceeded
    while waiting connection » : les candidats ICE n'annoncent que l'IP privée du conteneur, que
    le navigateur du site ne peut pas joindre. On ajoute donc l'adresse de l'hôte vue du site.

    En mode SITE, rien à ajouter : le conteneur est une machine du LAN, son IP est déjà la bonne —
    et annoncer une seconde adresse ne ferait qu'allonger la négociation.
    """
    from app.addressing import resolution_adresse_navigateur
    from app.database import node_net_interne
    node = _noeud_de_la_passerelle(node_id)
    if not node or not node_net_interne(node):
        return []
    adr = (resolution_adresse_navigateur(node) or {}).get("adresse")
    return [adr] if adr else []


def _gateway_params(node_id=None):
    proto = st.get("webrtc_ingest_proto") or "rtsp"
    return {
        "rtsp_port": int(st.get("webrtc_rtsp_port") or 8554),
        "http_port": int(st.get("webrtc_http_port") or 8889),
        "api_port": int(st.get("webrtc_api_port") or 9997),
        # Port du MÉDIA WebRTC (ICE), distinct de la signalisation : cf. `_hotes_ice`.
        "udp_port": int(st.get("webrtc_udp_port") or 8189),
        "additional_hosts": _hotes_ice(node_id),
        "mediamtx_version": MEDIAMTX_VERSION,
        # Horodatage absolu (useAbsoluteTimestamp) : seulement en ingestion RTSP — ffmpeg y émet des
        # SR RTCP ; en WHIP ce n'est pas vérifié, et sans SR MediaMTX jetterait TOUS les paquets.
        "absolute_timestamp": proto == "rtsp",
        # Opus relay of console listen buses (console_listen.py) — declared by the gateway only if
        # its image has ffmpeg/libopus (bobi-webrtc >= 0.6.1), reported as `listen_relay` on :8080.
        "listen_relay": True,
        "listen_relay_bitrate": "96k",
    }


def _record_observed_timestamping(live):
    """Persiste ce que la passerelle a RÉELLEMENT appliqué (rapport :8080 du script), pas l'intention :
    le hook de déploiement du streamer lit `webrtc_absolute_timestamp` pour choisir sa compensation
    A/V. Une passerelle encore en 1.9.3 (option inconnue) doit laisser la compensation en place."""
    db_set_setting("webrtc_absolute_timestamp", bool((live or {}).get("absolute_timestamp")))


def _eligible_docker_nodes():
    """Nœuds capables d'héberger la passerelle en Docker : réseau macvlan configuré (l'image
    bobi-webrtc est commune via le setting `webrtc_image`). Liste légère pour le sélecteur UI."""
    from app.database import db_get_nodes
    out = []
    has_img = bool(st.get("webrtc_image"))
    for n in db_get_nodes():
        if n.get("docker_network") and (n.get("webrtc_image") or has_img) and (n.get("status") or "") != "down":
            out.append({"id": n["id"], "name": n.get("name"),
                        "network": n.get("docker_network"),
                        "image": n.get("webrtc_image") or st.get("webrtc_image")})
    return out


def ingest_url(path, exterieur=False):
    """URL vers laquelle un encodeur pousse son flux (selon le proto choisi).

    `exterieur=False` (défaut) : adresse de CONTENEUR — c'est ce que consomment nos propres
    `streamer`, qui vivent sur le même réseau que la passerelle. Les faire passer par l'hôte
    serait un détour par le NAT, et surtout une dépendance à un port publié dont ils n'ont
    aucun besoin.
    `exterieur=True` : adresse pour un encodeur TIERS, affichée à un humain qui va la recopier
    dans son matériel — là, c'est l'hôte du nœud qu'il doit viser en « réseau interne ».
    """
    ip = _ip_navigateur() if exterieur else st.get("webrtc_gateway_ip")
    if not ip or not path:
        return None
    if (st.get("webrtc_ingest_proto") or "rtsp") == "whip":
        return f"http://{ip}:{int(st.get('webrtc_http_port') or 8889)}/{path}/whip"
    return f"rtsp://{ip}:{int(st.get('webrtc_rtsp_port') or 8554)}/{path}"


def _ip_navigateur():
    """IP de la passerelle TELLE QUE LA VOIT UN NAVIGATEUR (ou un encodeur du site).

    En « réseau interne » (bridge privé au nœud), l'IP du conteneur ne veut rien dire pour un
    navigateur : l'URL aboutirait nulle part et la lecture resterait noire SANS message. Le
    port publié, lui, aboutit sur l'hôte du nœud — cf. `app/addressing.adresse_navigateur` et
    `docs/chantiers/RESEAU_CONTENEURS.md`.

    ⚠ À n'utiliser QUE pour ce qui est joint depuis l'EXTÉRIEUR. L'orchestrateur, lui, parle à
    la passerelle sur son IP de conteneur (`:8080`, `:9997`) : ces ports ne sont pas publiés, et
    ils n'ont pas à l'être.
    """
    from app.addressing import adresse_navigateur
    return adresse_navigateur(st.get("webrtc_gateway_ip"),
                              vmid=st.get("webrtc_gateway_vmid") or None)


def whep_url(path):
    """URL WHEP (lecture) / page de preview pour un path donné — joint par le NAVIGATEUR."""
    ip = _ip_navigateur()
    if not ip or not path:
        return None
    return f"http://{ip}:{int(st.get('webrtc_http_port') or 8889)}/{path}"


def _wait_mediamtx_running_iter(ip):
    """Boucle d'attente commune (LXC/Docker) : poll :8080 jusqu'à running/erreur.
    Yield des lignes de progression ; termine SANS ligne finale (l'appelant conclut)."""
    import time
    running = False
    version = None
    last_err = None
    for i in range(45):  # ~90s
        time.sleep(2)
        try:
            r = requests.get(f"http://{ip}:8080/", timeout=2)
            j = r.json() if r.status_code == 200 else {}
            if j.get("running"):
                running, version = True, j.get("version")
                break
            if j.get("error"):
                last_err = j.get("error")
                break
            if i % 3 == 0:
                yield ("progress", f"  … en attente ({2 * (i + 1)}s)")
        except Exception:
            if i % 3 == 0:
                yield ("progress", f"  … métriques injoignables ({2 * (i + 1)}s)")
    yield ("done", running, version, last_err)


def _deploy_gateway_docker_iter(node_id):
    """Déploiement Docker de la passerelle : conteneur compute (image bobi-webrtc, MediaMTX
    pré-baké) sur un nœud macvlan. Réutilise tout le chemin compute (docker run + agent :8081)
    via deployer_script ; l'IP est attribuée par l'IPAM Docker (suivi des IP libres garanti par le
    driver macvlan) et persistée. Même contrat streamé (✅/❌)."""
    node = db_get_node(node_id)
    if not node:
        yield f"❌ Nœud {node_id} introuvable."
        return
    if not node.get("docker_network"):
        yield (f"❌ Nœud {node.get('name')} : réseau Docker (macvlan) non configuré "
               f"(Déploiement → Nœuds → Configuration).")
        return
    image = node.get("webrtc_image") or st.get("webrtc_image") or "bobi-webrtc:0.1"
    # Pré-check de PRÉSENCE RÉELLE de l'image sur le nœud (pas seulement le setting non vide) → message
    # actionnable au lieu d'un `docker run` qui échoue (« pull access denied ») + faux « déployé ».
    import shlex
    from app.host_ops import ssh_run
    rc_img, _o, _e = ssh_run(node["host"],
                             f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1", timeout=20)
    if rc_img != 0:
        yield (f"❌ Image {image} absente sur {node['name']}. Construis-la d'abord : Réglages → "
               f"Déploiement → onglet Build (règle « Hôte de build » sur un nœud Docker, ex. "
               f"{node['name']}) puis Construire/Pousser, et redéploie la passerelle.")
        return

    yield f"Déploiement de la passerelle WebRTC (MediaMTX) en conteneur Docker sur {node['name']}…"

    current = int(st.get("webrtc_gateway_vmid") or 0)
    existing = db_get_container(current) if current else None
    # Réutiliser la passerelle en place quand elle est sur CE nœud. L'ancien test lisait une colonne
    # `backend` qui n'existe plus (retrait du LXC) : il valait toujours « lxc », si bien que chaque
    # redéploiement CRÉAIT un second conteneur à une autre IP — les encodeurs, qui poussent vers
    # l'IP figée à leur déploiement, continuaient vers l'ancienne passerelle (constaté 2026-10-12).
    if existing and existing.get("docker_name") and int(existing.get("node_id") or 0) == int(node_id):
        target = current
        yield f"Réutilisation du conteneur passerelle Docker #{target}."
    else:
        from app.docker_compute import creer_container_compute
        target = creer_container_compute(node_id, _TYPE, hostname="webrtc-gateway")
        if not target:
            yield "❌ Création de la ligne conteneur échouée (voir alertes)."
            return
        # POIDS CPU « Arrière-plan » (cf. app/cpu_weight.py) : la passerelle relaie des aperçus,
        # elle ne doit pas disputer le CPU à un flux d'antenne quand le nœud sature. Posé à la
        # création seulement — le choix de l'exploitant est gardé.
        try:
            from app import cpu_weight as _cw
            _cw.poser_defaut(target, "background")
        except Exception as _exc:                                            # noqa: BLE001
            # `_e` est déjà pris plus haut dans cette fonction (dépaquetage de `ssh_run`) : une
            # liaison `except ... as _e` l'EFFACERAIT à la sortie du bloc.
            log.warning("default cpu weight for the gateway could not be set: %s", _exc)
        yield f"  → conteneur Docker #{target} enregistré."

    yield "docker run (image bobi-webrtc) + push du script MediaMTX via l'agent…"
    # ⚠ `node_id` EXPLICITE : la passerelle n'est pas encore en base sur ce nœud, et sans lui les
    # hôtes ICE seraient ceux du nœud précédent (cf. `_noeud_de_la_passerelle`).
    params   = _gateway_params(node_id)
    hostname = (db_get_container(target) or {}).get("hostname") or "webrtc-gateway"
    ok = deployer_script(target, _TYPE, params, script_content=_render_script(params, hostname))
    if not ok:
        yield "❌ docker run / déploiement du script échoué (voir alertes : image présente ? réseau macvlan ?)."
        return
    # Succès du `docker run` → c'est seulement MAINTENANT que la passerelle est « déployée ».
    db_set_setting("webrtc_gateway_vmid", int(target))

    c = db_get_container(target) or {}
    ip = c.get("docker_ip") or c.get("ip")
    if not ip:
        yield "❌ Pas d'IP macvlan attribuée — vérifiez le réseau Docker du nœud."
        return
    db_set_setting("webrtc_gateway_ip", ip)
    yield f"IP de la passerelle (macvlan) : {ip}"

    yield "Attente du démarrage de MediaMTX (binaire pré-baké → immédiat)…"
    running = version = last_err = None
    for ev in _wait_mediamtx_running_iter(ip):
        if ev[0] == "progress":
            yield ev[1]
        else:
            _, running, version, last_err = ev
    if running:
        _record_observed_timestamping(_live(ip))
        st.set("webrtc_enabled", True)
        yield "WebRTC activé automatiquement (résolution des destinations encodeurs)."
        yield f"✅ Passerelle WebRTC opérationnelle (Docker) sur #{target} ({ip}) — MediaMTX v{version}."
    elif last_err:
        yield f"❌ MediaMTX n'a pas démarré : {last_err}"
    else:
        yield "❌ MediaMTX n'a pas démarré dans le délai imparti — voir l'état live de l'onglet."


def deploy_gateway_iter(node_id=None):
    """Déploiement de la passerelle WebRTC en conteneur Docker sur un nœud (full-Docker ; le chemin
    LXC/template 299 a été retiré). Generator de lignes de progression, terminé par une ligne ✅/❌."""
    if not node_id:
        yield "❌ Choisis un nœud Docker (déploiement de la passerelle en Docker uniquement)."
        return
    yield from _deploy_gateway_docker_iter(node_id)


def _live(ip):
    """Rapport :8080 du script de la passerelle (None si injoignable)."""
    try:
        r = requests.get(f"http://{ip}:8080/", timeout=2)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _paths_detail(ip, api_port):
    """Détail des paths actifs via l'API MediaMTX (publisher + nb de lecteurs)."""
    try:
        r = requests.get(f"http://{ip}:{api_port}/v3/paths/list", timeout=1.5)
        if r.status_code != 200:
            return None
        items = (r.json() or {}).get("items") or []
        out = []
        for it in items:
            src = it.get("source") or {}
            out.append({
                "name": it.get("name"),
                "ready": bool(it.get("ready")),
                "source": src.get("type") or "—",
                "readers": len(it.get("readers") or []),
                "bytes_received": it.get("bytesReceived"),
                "bytes_sent": it.get("bytesSent"),
            })
        return out
    except Exception:
        return None


def status():
    """État de la passerelle : settings + container + métriques live :8080 + URLs +
    détail des paths (API MediaMTX). Conçu pour un affichage riche dans l'onglet."""
    vmid = int(st.get("webrtc_gateway_vmid") or 0)
    rtsp_p = int(st.get("webrtc_rtsp_port") or 8554)
    http_p = int(st.get("webrtc_http_port") or 8889)
    api_p = int(st.get("webrtc_api_port") or 9997)
    out = {
        "settings": {
            "enabled": bool(st.get("webrtc_enabled")),
            "vmid": vmid,
            "ip": st.get("webrtc_gateway_ip") or "",
            "ingest_proto": st.get("webrtc_ingest_proto") or "rtsp",
            "rtsp_port": rtsp_p,
            "http_port": http_p,
            "udp_port": int(st.get("webrtc_udp_port") or 8189),
            "ice_hosts": _hotes_ice(),
            "api_port": api_p,
            "mediamtx_version": MEDIAMTX_VERSION,
            "absolute_timestamp": bool(st.get("webrtc_absolute_timestamp")),
            "image": st.get("webrtc_image") or "bobi-webrtc:0.1",
            "node_id": int(st.get("webrtc_node_id") or 0),
            "script_restart_alert_threshold": int(st.get("script_restart_alert_threshold") or 3),
        },
        "docker_nodes": _eligible_docker_nodes(),
        # `running` : le conteneur de la passerelle TOURNE. Distinct de
        # `deployed` (il existe en base) et de `live` (l'API MediaMTX a répondu).
        # Les trois sont utiles, mais c'est celui-ci que la page Services lit —
        # sans lui elle ne pouvait rien affirmer, et affichait un tiret.
        "running": False,
        "deployed": False,
        "container_status": None,
        "hostname": None,
        "live": None,
        "urls": None,
        "paths_detail": None,
    }
    if not vmid:
        return out
    c = db_get_container(vmid)
    if c:
        out["deployed"] = True
        out["container_status"] = c.get("status")
        out["running"] = (c.get("status") == "running")
        out["hostname"] = c.get("hostname")
    # Ces URL sont LUES PAR UN HUMAIN pour configurer un encodeur ou ouvrir une page : adresse
    # extérieure, donc (l'hôte du nœud en « réseau interne »).
    ip = _ip_navigateur() or (c or {}).get("ip")
    if ip:
        proto = st.get("webrtc_ingest_proto") or "rtsp"
        ingest = (f"http://{ip}:{http_p}/<path>/whip" if proto == "whip"
                  else f"rtsp://{ip}:{rtsp_p}/<path>")
        out["urls"] = {
            "ingest": ingest,
            "whep": f"http://{ip}:{http_p}/<path>/whep",
            "player": f"http://{ip}:{http_p}/<path>",
            "api": f"http://{ip}:{api_p}",
        }
        try:
            r = requests.get(f"http://{ip}:8080/", timeout=1.5)
            if r.status_code == 200:
                out["live"] = r.json()
        except Exception:
            out["live"] = None
        out["paths_detail"] = _paths_detail(ip, api_p)
    return out


def destroy_gateway():
    """Détruit le container passerelle et réinitialise les settings associés.
    Retourne (ok, message). Bloquant — appeler dans un thread."""
    vmid = int(st.get("webrtc_gateway_vmid") or 0)
    if not vmid:
        return False, "aucune passerelle déployée"
    c = db_get_container(vmid)
    if not c:
        # Container déjà absent : on nettoie juste les settings.
        st.set("webrtc_gateway_vmid", 0)
        st.set("webrtc_gateway_ip", "")
        st.set("webrtc_enabled", False)
        st.set("webrtc_absolute_timestamp", False)
        return True, f"container #{vmid} déjà absent — settings réinitialisés"
    from app.containers import detruire_container
    detruire_container(vmid)
    st.set("webrtc_gateway_vmid", 0)
    st.set("webrtc_gateway_ip", "")
    st.set("webrtc_enabled", False)
    st.set("webrtc_absolute_timestamp", False)
    return True, f"container #{vmid} détruit, WebRTC désactivé"


# ─── Core plugin manifest ─────────────────────────────────────────────

__manifest__ = {
    "id":            "webrtc_gateway",
    "label":         "WebRTC",
    "nav_tab":       "webrtc",
    "tab_template":  "settings_tabs/webrtc.html",
    "settings_keys": {
        "webrtc_enabled":                  {"type": "bool", "default": False},
        "webrtc_gateway_vmid":             {"type": "int",  "default": 0},
        "webrtc_gateway_ip":               {"type": "str",  "default": ""},
        "webrtc_ingest_proto":             {"type": "str",  "default": "rtsp"},
        "webrtc_rtsp_port":                {"type": "int",  "default": 8554},
        "webrtc_http_port":                {"type": "int",  "default": 8889},
        "webrtc_udp_port":                 {"type": "int",  "default": 8189},
        "webrtc_api_port":                 {"type": "int",  "default": 9997},
        "webrtc_mediamtx_version":         {"type": "str",  "default": MEDIAMTX_VERSION},
        "webrtc_absolute_timestamp":       {"type": "bool", "default": False},
        "webrtc_image":                    {"type": "str",  "default": "bobi-webrtc:0.1"},
        "webrtc_node_id":                  {"type": "int",  "default": 0},
        "script_restart_alert_threshold":  {"type": "int",  "default": 3},
    },
}


def register_routes(bp):
    from flask import request, jsonify, Response, stream_with_context
    from app.auth import require_login, require_perm
    from app.database import db_set_setting

    @bp.route("/api/webrtc/status", methods=["GET"])
    @require_login
    def webrtc_status():
        return jsonify(status())

    @bp.route("/api/webrtc/deploy", methods=["POST"])
    @require_perm("settings.edit")
    def webrtc_deploy():
        body = request.get_json(force=True, silent=True) or {}
        node_id = body.get("node_id")
        try:
            node_id = int(node_id) if node_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            node_id = None
        # Mémorise le dernier nœud choisi (pré-sélection UI au prochain déploiement).
        if node_id:
            db_set_setting("webrtc_node_id", node_id)
        def gen():
            try:
                for line in deploy_gateway_iter(node_id=node_id):
                    yield line + "\n"
            except Exception as e:
                yield f"❌ Erreur inattendue : {e}\n"
        return Response(stream_with_context(gen()),
                        mimetype="text/plain; charset=utf-8",
                        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @bp.route("/api/webrtc/destroy", methods=["POST"])
    @require_perm("settings.edit")
    def webrtc_destroy():
        def _run():
            ok, msg = destroy_gateway()
            from app.database import db_add_alert
            db_add_alert(f"Passerelle WebRTC : {msg}", "info" if ok else "warning", kind="deploy")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "suppression_en_cours"})

    # ── Console listen, compressed (Opus/WebRTC) — cf. console_listen.py ─────────────────────────
    from . import console_listen as _cl

    def _uid():
        from app.auth import current_user
        return (current_user() or {}).get("id")

    def _reply(code, body, headers):
        if isinstance(body, dict):
            resp = jsonify(body)
            resp.status_code = code
        else:
            resp = Response(body or b"", status=code)
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp

    @bp.route("/api/webrtc/console-listen/<int:vmid>", methods=["GET"])
    @require_login
    def webrtc_console_listen(vmid):
        denied = _cl.deny(vmid)
        if denied:
            return denied
        code, info = _cl.resolve(vmid, request.args.get("bus") or None)
        info.pop("_gateway", None)
        return jsonify(info), code

    @bp.route("/api/webrtc/console-listen/<int:vmid>/whep", methods=["POST"])
    @require_login
    def webrtc_console_listen_whep(vmid):
        denied = _cl.deny(vmid)
        if denied:
            return denied
        sdp = request.get_data(cache=False, as_text=True)
        return _reply(*_cl.offer(vmid, request.args.get("bus") or None, sdp, user=_uid()))

    @bp.route("/api/webrtc/console-listen/session/<token>", methods=["PATCH", "DELETE"])
    @require_login
    def webrtc_console_listen_session(token):
        return _reply(*_cl.session(token, request.method, request.get_data(cache=False),
                                   request.headers.get("Content-Type"), user=_uid()))

    @bp.route("/api/webrtc/apply", methods=["POST"])
    @require_perm("settings.edit")
    def webrtc_apply():
        data  = request.json or {}
        proto = (data.get("ingest_proto") or "rtsp")
        if proto not in ("rtsp", "whip"):
            return jsonify({"ok": False, "error": "proto invalide (rtsp|whip)"}), 400
        db_set_setting("webrtc_enabled",      bool(data.get("enabled")))
        db_set_setting("webrtc_ingest_proto", proto)
        for k in ("rtsp_port", "http_port", "udp_port", "api_port"):
            if k in data:
                try:
                    db_set_setting("webrtc_" + k, int(data[k]))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": f"{k} doit être un entier"}), 400
        wanted = str(data.get("mediamtx_version") or "").strip().lstrip("v")
        if wanted and wanted != MEDIAMTX_VERSION:
            return jsonify({"ok": False, "error": f"version de MediaMTX fixée par le service "
                                                  f"({MEDIAMTX_VERSION}) : {wanted} refusée"}), 400
        db_set_setting("webrtc_mediamtx_version", MEDIAMTX_VERSION)
        if "script_restart_alert_threshold" in data:
            try:
                db_set_setting("script_restart_alert_threshold",
                               max(1, int(data["script_restart_alert_threshold"])))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "script_restart_alert_threshold doit être un entier"}), 400
        vmid = int(st.get("webrtc_gateway_vmid") or 0)
        redeployed = False
        if vmid and db_get_container(vmid):
            params   = _gateway_params()
            hostname = (db_get_container(vmid) or {}).get("hostname") or f"mxl{vmid}"
            content = _render_script(params, hostname)
            ip = st.get("webrtc_gateway_ip") or (db_get_container(vmid) or {}).get("ip")

            def _redeploy():
                # Tant que le nouveau script n'a pas parlé, on ne sait pas ce qu'il applique.
                db_set_setting("webrtc_absolute_timestamp", False)
                if deployer_script(vmid, _TYPE, params, script_content=content) and ip:
                    for ev in _wait_mediamtx_running_iter(ip):
                        if ev[0] == "done" and ev[1]:
                            _record_observed_timestamping(_live(ip))
            threading.Thread(target=_redeploy, daemon=True).start()
            redeployed = True
        return jsonify({"ok": True, "redeployed": redeployed})
